import asyncio
import json
import os
import mimetypes
import re
import aiohttp
from io import BytesIO
from nio import (
    AsyncClient, RoomMessageText, RoomMessageEmote, RoomMessageNotice,
    RoomMessageMedia, RoomMessageFile, RoomMessageImage, RoomMessageVideo,
    RoomMessageAudio, RoomMessage, MatrixRoom, UploadResponse, RedactionEvent
)
import discord
from discord import File as DiscordFile, MessageReference
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, MessageHandler, filters
from telegram.constants import ParseMode

# Carrega configurações
with open('config.json', 'r') as f:
    config = json.load(f)

# ---------------- CONFIGURAÇÕES ----------------
MATRIX_HOMESERVER = config['matrix']['homeserver']
MATRIX_USERNAME = config['matrix']['username']
MATRIX_PASSWORD = config['matrix']['password']

DISCORD_TOKEN = config['discord']['token']
TELEGRAM_TOKEN = config['telegram']['token']

BRIDGES = config['bridges']

STATE_FILE = config.get('state_file', 'bot_state.json')
MESSAGE_MAP_FILE = 'message_map.json'

# ---------------- CLIENTES GLOBAIS ----------------
matrix_client = None
discord_client = discord.Client(intents=discord.Intents.all())
telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
telegram_bot = telegram_app.bot

# ---------------- MAPEAMENTOS DE BRIDGES ----------------
matrix_to_bridge = {}
discord_to_bridge = {}
telegram_to_bridge = {}

for bridge in BRIDGES:
    room_id = bridge['matrix_room']
    matrix_to_bridge[room_id] = bridge
    for ch in bridge.get('discord_channels', []):
        discord_to_bridge[ch] = bridge
    for tg in bridge.get('telegram_chats', []):
        telegram_to_bridge[tg] = bridge

# ---------------- PERSISTÊNCIA DE TIMESTAMPS (por sala) ----------------
def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

# ---------------- PERSISTÊNCIA DO MAPEAMENTO DE MENSAGENS ----------------
def load_message_map():
    try:
        with open(MESSAGE_MAP_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_message_map(map_data):
    with open(MESSAGE_MAP_FILE, 'w') as f:
        json.dump(map_data, f)

# ---------------- FUNÇÕES AUXILIARES ----------------
async def matrix_login():
    client = AsyncClient(MATRIX_HOMESERVER, MATRIX_USERNAME)
    resp = await client.login(MATRIX_PASSWORD)
    if hasattr(resp, 'access_token'):
        print("[Matrix] Login bem-sucedido")
        return client
    else:
        print(f"[Matrix] Falha no login: {resp}")
        return None

async def download_file(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.read()
                content_type = resp.headers.get('Content-Type', 'application/octet-stream')
                return data, content_type
    return None, None

async def upload_to_matrix(file_data, filename, content_type):
    """
    Faz upload de um arquivo para o Matrix.
    file_data pode ser bytes, bytearray ou BytesIO.
    Retorna a URL mxc:// ou None.
    """
    if isinstance(file_data, (bytes, bytearray)):
        file_data = BytesIO(file_data)
    elif not isinstance(file_data, BytesIO):
        try:
            file_data = BytesIO(file_data)
        except:
            print("[Matrix] Tipo de arquivo não suportado para upload")
            return None

    resp = await matrix_client.upload(
        data_provider=file_data,
        content_type=content_type,
        filename=filename,
        filesize=file_data.getbuffer().nbytes
    )
    if isinstance(resp, UploadResponse):
        return resp.content_uri
    else:
        print(f"[Matrix] Falha no upload: {resp}")
        return None

def get_matrix_display_name(room, user_id):
    if room and hasattr(room, 'users'):
        user_info = room.users.get(user_id)
        if user_info and user_info.display_name:
            return user_info.display_name
    return user_id.split(':')[0].lstrip('@')

async def download_matrix_file(client, url):
    if url.startswith('mxc://'):
        parts = url[6:].split('/')
        server_name = parts[0]
        media_id = parts[1]
        resp = await client.download(server_name, media_id)
        if hasattr(resp, 'body'):
            return resp.body, resp.filename if hasattr(resp, 'filename') else None
    return None, None

def escape_html(text):
    """Escapa caracteres especiais HTML."""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def markdown_to_html(text):
    """Converte **texto** para <b>texto</b> de forma simples."""
    return re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)

# ---------------- FUNÇÕES DE EDIÇÃO ----------------
async def send_matrix_edit(room_id, event_id, new_content):
    """Envia uma edição para o Matrix (m.replace)."""
    content = {
        "msgtype": "m.text",
        "body": f" * {new_content['body']}",
        "m.new_content": new_content,
        "m.relates_to": {
            "rel_type": "m.replace",
            "event_id": event_id
        }
    }
    await matrix_client.room_send(room_id, "m.room.message", content)

async def send_discord_edit(channel_id, message_id, new_text):
    """Edita uma mensagem no Discord."""
    channel = discord_client.get_channel(channel_id)
    if channel:
        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(content=new_text)
        except Exception as e:
            print(f"[Discord] Erro ao editar mensagem {message_id}: {e}")

async def send_telegram_edit(chat_id, message_id, new_text):
    """Edita uma mensagem no Telegram."""
    try:
        await telegram_bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=new_text,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print(f"[Telegram] Erro ao editar mensagem {message_id}: {e}")

# ---------------- FUNÇÕES DE DELEÇÃO CRUZADA ----------------
async def handle_matrix_redaction(room: MatrixRoom, event: RedactionEvent):
    if event.sender == matrix_client.user_id:
        return

    redacts = event.redacts
    if not redacts:
        return

    msg_map = load_message_map()
    if redacts not in msg_map:
        return

    target = msg_map[redacts]
    bridge = matrix_to_bridge.get(room.room_id)
    if not bridge:
        return

    if target['platform'] == 'discord':
        channel_id = target['channel_id']
        message_id = target['message_id']
        channel = discord_client.get_channel(channel_id)
        if channel:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.delete()
                print(f"[Matrix -> Discord] Mensagem apagada: {message_id}")
                # Remove do mapa
                del msg_map[redacts]
                rev_key = str(message_id)
                if rev_key in msg_map:
                    del msg_map[rev_key]
                save_message_map(msg_map)
            except Exception as e:
                print(f"[Matrix -> Discord] Erro ao apagar: {e}")

    elif target['platform'] == 'telegram':
        chat_id = target['chat_id']
        message_id = target['message_id']
        try:
            await telegram_bot.delete_message(chat_id=chat_id, message_id=message_id)
            print(f"[Matrix -> Telegram] Mensagem apagada: {message_id}")
            del msg_map[redacts]
            rev_key = str(message_id)
            if rev_key in msg_map:
                del msg_map[rev_key]
            save_message_map(msg_map)
        except Exception as e:
            print(f"[Matrix -> Telegram] Erro ao apagar: {e}")

@discord_client.event
async def on_message_delete(message):
    if message.author.id != discord_client.user.id:
        return

    msg_map = load_message_map()
    msg_id_str = str(message.id)
    if msg_id_str not in msg_map:
        return

    target = msg_map[msg_id_str]
    if target['platform'] == 'matrix':
        room_id = target['room_id']
        event_id = target['event_id']
        try:
            await matrix_client.room_redact(room_id, event_id, reason="Deleted via bridge")
            print(f"[Discord -> Matrix] Mensagem redacionada: {event_id}")
            del msg_map[msg_id_str]
            if event_id in msg_map:
                del msg_map[event_id]
            save_message_map(msg_map)
        except Exception as e:
            print(f"[Discord -> Matrix] Erro ao redacionar: {e}")
    elif target['platform'] == 'telegram':
        chat_id = target['chat_id']
        message_id = target['message_id']
        try:
            await telegram_bot.delete_message(chat_id=chat_id, message_id=message_id)
            print(f"[Discord -> Telegram] Mensagem apagada: {message_id}")
            del msg_map[msg_id_str]
            rev_key = str(message_id)
            if rev_key in msg_map:
                del msg_map[rev_key]
            save_message_map(msg_map)
        except Exception as e:
            print(f"[Discord -> Telegram] Erro ao apagar: {e}")

# ---------------- CALLBACK MATRIX ----------------
async def matrix_message_callback(room: MatrixRoom, event: RoomMessage):
    if event.sender == matrix_client.user_id:
        return

    if room.room_id not in matrix_to_bridge:
        return
    bridge = matrix_to_bridge[room.room_id]

    content = getattr(event, 'source', {}).get('content', {})
    relates_to = content.get('m.relates_to', {})

    # Verifica se é edição
    if relates_to.get('rel_type') == 'm.replace':
        original_event_id = relates_to.get('event_id')
        new_content = content.get('m.new_content', {})
        new_body = new_content.get('body', '')

        msg_map = load_message_map()
        if original_event_id in msg_map:
            target = msg_map[original_event_id]
            sender_display = get_matrix_display_name(room, event.sender)
            if target['platform'] == 'discord':
                new_text = f"**{sender_display}:** {new_body}"
                await send_discord_edit(target['channel_id'], target['message_id'], new_text)
            elif target['platform'] == 'telegram':
                new_text = f"<b>{escape_html(sender_display)}:</b> {escape_html(new_body)}"
                await send_telegram_edit(target['chat_id'], target['message_id'], new_text)
        return

    # Ignora outros relates (como reações)
    if relates_to and 'rel_type' in relates_to:
        return

    ts = getattr(event, 'server_timestamp', 0)
    state = load_state()
    last_ts = state.get(room.room_id, 0)
    if ts <= last_ts:
        return
    state[room.room_id] = ts
    save_state(state)

    sender_display = get_matrix_display_name(room, event.sender)

    reply_to_event_id = None
    if 'm.in_reply_to' in relates_to:
        reply_to_event_id = relates_to['m.in_reply_to'].get('event_id')

    text_to_send = None
    file_to_send = None
    filename = None
    content_type = None

    if isinstance(event, (RoomMessageText, RoomMessageEmote, RoomMessageNotice)):
        msgtype = getattr(event, 'msgtype', 'm.text')
        body = getattr(event, 'body', '')
        if msgtype == 'm.emote':
            text_to_send = f"* {sender_display} {body}"
        else:
            text_to_send = f"**{sender_display}:** {body}"

    elif isinstance(event, (RoomMessageImage, RoomMessageVideo, RoomMessageAudio, RoomMessageFile, RoomMessageMedia)):
        url = getattr(event, 'url', None)
        body = getattr(event, 'body', '')
        if url:
            data, fname = await download_matrix_file(matrix_client, url)
            if data:
                if not fname:
                    ext = mimetypes.guess_extension(getattr(event, 'mimetype', 'application/octet-stream')) or '.bin'
                    fname = f"media_{ts}{ext}"
                file_to_send = data
                filename = fname
                content_type = getattr(event, 'mimetype', 'application/octet-stream')
                if body:
                    text_to_send = f"**{sender_display}:** {body}"
                else:
                    text_to_send = f"**{sender_display}** enviou um arquivo"
            else:
                text_to_send = f"**{sender_display}** enviou uma mídia (falha no download)"
        else:
            text_to_send = f"**{sender_display}** enviou uma mídia sem URL"

    elif hasattr(event, 'msgtype') and event.msgtype == 'm.sticker':
        url = getattr(event, 'url', None)
        if url:
            data, fname = await download_matrix_file(matrix_client, url)
            if data:
                file_to_send = data
                filename = fname or f"sticker_{ts}.png"
                content_type = getattr(event, 'mimetype', 'image/png')
                text_to_send = f"**{sender_display}** enviou um sticker"
            else:
                text_to_send = f"**{sender_display}** enviou um sticker (falha no download)"
        else:
            text_to_send = f"**{sender_display}** enviou um sticker sem URL"

    if not text_to_send and not file_to_send:
        return

    msg_map = load_message_map()

    reply_target_discord = None
    reply_target_telegram = None
    if reply_to_event_id and reply_to_event_id in msg_map:
        target_info = msg_map[reply_to_event_id]
        if target_info['platform'] == 'discord':
            reply_target_discord = target_info['message_id']
        elif target_info['platform'] == 'telegram':
            reply_target_telegram = target_info['message_id']

    # Envia para Discord
    for channel_id in bridge.get('discord_channels', []):
        channel = discord_client.get_channel(channel_id)
        if channel:
            try:
                kwargs = {}
                if reply_target_discord:
                    kwargs['reference'] = MessageReference(message_id=reply_target_discord, channel_id=channel_id)
                if file_to_send:
                    discord_file = DiscordFile(fp=BytesIO(file_to_send), filename=filename)
                    sent = await channel.send(content=text_to_send, file=discord_file, **kwargs)
                else:
                    sent = await channel.send(text_to_send, **kwargs)
                msg_map[event.event_id] = {'platform': 'discord', 'channel_id': channel_id, 'message_id': sent.id}
                msg_map[str(sent.id)] = {'platform': 'matrix', 'room_id': room.room_id, 'event_id': event.event_id}
                print(f"[Matrix -> Discord {channel_id}] OK")
            except Exception as e:
                print(f"[Matrix -> Discord {channel_id}] Erro: {e}")

    # Envia para Telegram
    for chat_id in bridge.get('telegram_chats', []):
        try:
            kwargs = {}
            if reply_target_telegram:
                kwargs['reply_to_message_id'] = reply_target_telegram
            if file_to_send:
                file_bytesio = BytesIO(file_to_send)
                file_bytesio.name = filename
                # Converte texto para HTML para o Telegram
                caption_html = markdown_to_html(text_to_send)
                if content_type and content_type.startswith('image/'):
                    sent = await telegram_bot.send_photo(
                        chat_id=chat_id,
                        photo=file_bytesio,
                        caption=caption_html,
                        parse_mode=ParseMode.HTML,
                        **kwargs
                    )
                elif content_type and content_type.startswith('video/'):
                    sent = await telegram_bot.send_video(
                        chat_id=chat_id,
                        video=file_bytesio,
                        caption=caption_html,
                        parse_mode=ParseMode.HTML,
                        **kwargs
                    )
                elif content_type and content_type.startswith('audio/'):
                    sent = await telegram_bot.send_audio(
                        chat_id=chat_id,
                        audio=file_bytesio,
                        caption=caption_html,
                        parse_mode=ParseMode.HTML,
                        **kwargs
                    )
                else:
                    sent = await telegram_bot.send_document(
                        chat_id=chat_id,
                        document=file_bytesio,
                        caption=caption_html,
                        filename=filename,
                        parse_mode=ParseMode.HTML,
                        **kwargs
                    )
            else:
                # Texto puro: converte para HTML
                text_html = markdown_to_html(text_to_send)
                sent = await telegram_bot.send_message(
                    chat_id=chat_id,
                    text=text_html,
                    parse_mode=ParseMode.HTML,
                    **kwargs
                )
            msg_map[event.event_id] = {'platform': 'telegram', 'chat_id': chat_id, 'message_id': sent.message_id}
            msg_map[str(sent.message_id)] = {'platform': 'matrix', 'room_id': room.room_id, 'event_id': event.event_id}
            print(f"[Matrix -> Telegram {chat_id}] OK")
        except Exception as e:
            print(f"[Matrix -> Telegram {chat_id}] Erro: {e}")

    save_message_map(msg_map)

# ---------------- CALLBACK DISCORD ----------------
@discord_client.event
async def on_ready():
    print(f"[Discord] Conectado como {discord_client.user}")

@discord_client.event
async def on_message(message):
    if message.author.id == discord_client.user.id:
        return

    if message.channel.id not in discord_to_bridge:
        return
    bridge = discord_to_bridge[message.channel.id]

    author_display = message.author.display_name

    reply_to_msg_id = None
    if message.reference and message.reference.resolved:
        referenced = message.reference.resolved
        if isinstance(referenced, discord.Message):
            reply_to_msg_id = referenced.id

    msg_map = load_message_map()

    reply_target_matrix = None
    reply_target_telegram = None
    if reply_to_msg_id and str(reply_to_msg_id) in msg_map:
        target_info = msg_map[str(reply_to_msg_id)]
        if target_info['platform'] == 'matrix':
            reply_target_matrix = target_info['event_id']
        elif target_info['platform'] == 'telegram':
            reply_target_telegram = target_info['message_id']

    # Processa stickers do Discord
    if message.stickers:
        for sticker in message.stickers:
            sticker_url = sticker.url
            data, content_type = await download_file(sticker_url)
            if data:
                mxc_url = await upload_to_matrix(data, f"sticker_{sticker.id}.png", content_type or 'image/png')
                if mxc_url:
                    content = {
                        "msgtype": "m.image",
                        "body": f"Sticker: {sticker.name}",
                        "url": mxc_url,
                        "info": {
                            "mimetype": content_type or 'image/png',
                            "size": len(data)
                        }
                    }
                    if reply_target_matrix:
                        content['m.relates_to'] = {
                            'm.in_reply_to': {
                                'event_id': reply_target_matrix
                            }
                        }
                    # Adiciona formatação HTML para o Matrix
                    if message.content:
                        content['body'] = message.content
                        content['format'] = "org.matrix.custom.html"
                        content['formatted_body'] = f"<b>{author_display}:</b> {message.content}"
                    try:
                        await matrix_client.room_send(
                            room_id=bridge['matrix_room'],
                            message_type="m.room.message",
                            content=content
                        )
                        print(f"[Discord -> Matrix] Sticker enviado: {sticker.name}")
                    except Exception as e:
                        print(f"[Discord -> Matrix] Erro ao enviar sticker: {e}")

                # Telegram
                for chat_id in bridge.get('telegram_chats', []):
                    try:
                        kwargs = {}
                        if reply_target_telegram:
                            kwargs['reply_to_message_id'] = reply_target_telegram
                        file_bytesio = BytesIO(data)
                        file_bytesio.name = f"sticker_{sticker.id}.png"
                        sent = await telegram_bot.send_document(
                            chat_id=chat_id,
                            document=file_bytesio,
                            caption=f"<b>{escape_html(author_display)}</b> enviou um sticker",
                            parse_mode=ParseMode.HTML,
                            **kwargs
                        )
                        msg_map[str(sent.message_id)] = {'platform': 'discord', 'channel_id': message.channel.id, 'message_id': message.id}
                        msg_map[str(message.id)] = {'platform': 'telegram', 'chat_id': chat_id, 'message_id': sent.message_id}
                        save_message_map(msg_map)
                        print(f"[Discord -> Telegram {chat_id}] Sticker enviado")
                    except Exception as e:
                        print(f"[Discord -> Telegram {chat_id}] Erro: {e}")

    # Processa attachments (incluindo GIFs, imagens, vídeos, áudios)
    if message.attachments:
        for attachment in message.attachments:
            data, content_type = await download_file(attachment.url)
            if data:
                mxc_url = await upload_to_matrix(data, attachment.filename, content_type)
                if mxc_url:
                    msgtype = 'm.file'
                    if content_type.startswith('image/'):
                        msgtype = 'm.image'
                    elif content_type.startswith('video/'):
                        msgtype = 'm.video'
                    elif content_type.startswith('audio/'):
                        msgtype = 'm.audio'

                    content = {
                        "msgtype": msgtype,
                        "body": attachment.filename,
                        "url": mxc_url,
                        "info": {
                            "mimetype": content_type,
                            "size": len(data)
                        }
                    }
                    if message.content:
                        content['body'] = message.content
                        # Adiciona formatação HTML para o Matrix
                        content['format'] = "org.matrix.custom.html"
                        content['formatted_body'] = f"<b>{author_display}:</b> {message.content}"

                    if reply_target_matrix:
                        content['m.relates_to'] = {
                            'm.in_reply_to': {
                                'event_id': reply_target_matrix
                            }
                        }

                    try:
                        await matrix_client.room_send(
                            room_id=bridge['matrix_room'],
                            message_type="m.room.message",
                            content=content
                        )
                        print(f"[Discord -> Matrix] Mídia enviada: {attachment.filename}")
                    except Exception as e:
                        print(f"[Discord -> Matrix] Erro ao enviar mídia: {e}")

                # Telegram
                for chat_id in bridge.get('telegram_chats', []):
                    try:
                        kwargs = {}
                        if reply_target_telegram:
                            kwargs['reply_to_message_id'] = reply_target_telegram
                        file_bytesio = BytesIO(data)
                        file_bytesio.name = attachment.filename
                        caption_html = f"<b>{escape_html(author_display)}</b>"
                        if message.content:
                            caption_html += f" {escape_html(message.content)}"
                        if content_type.startswith('image/'):
                            sent = await telegram_bot.send_photo(
                                chat_id=chat_id,
                                photo=file_bytesio,
                                caption=caption_html,
                                parse_mode=ParseMode.HTML,
                                **kwargs
                            )
                        elif content_type.startswith('video/'):
                            sent = await telegram_bot.send_video(
                                chat_id=chat_id,
                                video=file_bytesio,
                                caption=caption_html,
                                parse_mode=ParseMode.HTML,
                                **kwargs
                            )
                        elif content_type.startswith('audio/'):
                            sent = await telegram_bot.send_audio(
                                chat_id=chat_id,
                                audio=file_bytesio,
                                caption=caption_html,
                                parse_mode=ParseMode.HTML,
                                **kwargs
                            )
                        else:
                            sent = await telegram_bot.send_document(
                                chat_id=chat_id,
                                document=file_bytesio,
                                caption=caption_html,
                                filename=attachment.filename,
                                parse_mode=ParseMode.HTML,
                                **kwargs
                            )
                        msg_map[str(sent.message_id)] = {'platform': 'discord', 'channel_id': message.channel.id, 'message_id': message.id}
                        msg_map[str(message.id)] = {'platform': 'telegram', 'chat_id': chat_id, 'message_id': sent.message_id}
                        save_message_map(msg_map)
                        print(f"[Discord -> Telegram {chat_id}] Mídia enviada")
                    except Exception as e:
                        print(f"[Discord -> Telegram {chat_id}] Erro: {e}")

    # Texto puro
    elif message.content:
        text_to_send = f"**{author_display}:** {message.content}"

        # Matrix com formatação
        content = {
            "msgtype": "m.text",
            "body": text_to_send,
            "format": "org.matrix.custom.html",
            "formatted_body": f"<b>{author_display}:</b> {escape_html(message.content)}"
        }
        if reply_target_matrix:
            content['m.relates_to'] = {
                'm.in_reply_to': {
                    'event_id': reply_target_matrix
                }
            }

        try:
            await matrix_client.room_send(
                room_id=bridge['matrix_room'],
                message_type="m.room.message",
                content=content
            )
            print(f"[Discord -> Matrix] OK")
        except Exception as e:
            print(f"[Discord -> Matrix] Erro: {e}")

        # Telegram
        for chat_id in bridge.get('telegram_chats', []):
            try:
                kwargs = {}
                if reply_target_telegram:
                    kwargs['reply_to_message_id'] = reply_target_telegram
                text_html = f"<b>{escape_html(author_display)}:</b> {escape_html(message.content)}"
                sent = await telegram_bot.send_message(
                    chat_id=chat_id,
                    text=text_html,
                    parse_mode=ParseMode.HTML,
                    **kwargs
                )
                msg_map[str(sent.message_id)] = {'platform': 'discord', 'channel_id': message.channel.id, 'message_id': message.id}
                msg_map[str(message.id)] = {'platform': 'telegram', 'chat_id': chat_id, 'message_id': sent.message_id}
                save_message_map(msg_map)
                print(f"[Discord -> Telegram {chat_id}] OK")
            except Exception as e:
                print(f"[Discord -> Telegram {chat_id}] Erro: {e}")

@discord_client.event
async def on_message_edit(before, after):
    if after.author.id == discord_client.user.id:
        return
    if before.content == after.content:
        return
    if after.channel.id not in discord_to_bridge:
        return
    bridge = discord_to_bridge[after.channel.id]

    msg_map = load_message_map()
    msg_id_str = str(after.id)
    if msg_id_str not in msg_map:
        return

    target = msg_map[msg_id_str]
    author_display = after.author.display_name
    if target['platform'] == 'matrix':
        room_id = target['room_id']
        event_id = target['event_id']
        new_content = {
            "msgtype": "m.text",
            "body": f"**{author_display}:** {after.content}",
            "format": "org.matrix.custom.html",
            "formatted_body": f"<b>{author_display}:</b> {after.content}"
        }
        await send_matrix_edit(room_id, event_id, new_content)
        print(f"[Discord -> Matrix] Mensagem editada: {after.id}")
    elif target['platform'] == 'telegram':
        chat_id = target['chat_id']
        message_id = target['message_id']
        new_text = f"<b>{escape_html(author_display)}:</b> {escape_html(after.content)}"
        await send_telegram_edit(chat_id, message_id, new_text)
        print(f"[Discord -> Telegram] Mensagem editada: {after.id}")

# ---------------- CALLBACK TELEGRAM ----------------
async def telegram_message_callback(update: Update, context):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    if chat_id not in telegram_to_bridge:
        return
    bridge = telegram_to_bridge[chat_id]

    user = update.effective_user
    author_display = user.full_name or user.first_name

    reply_to_msg_id = None
    if update.message.reply_to_message:
        reply_to_msg_id = update.message.reply_to_message.message_id

    msg_map = load_message_map()

    reply_target_matrix = None
    reply_target_discord = None
    if reply_to_msg_id and str(reply_to_msg_id) in msg_map:
        target_info = msg_map[str(reply_to_msg_id)]
        if target_info['platform'] == 'matrix':
            reply_target_matrix = target_info['event_id']
        elif target_info['platform'] == 'discord':
            reply_target_discord = target_info['message_id']

    # Processa mídia
    file_to_send = None
    caption = update.message.caption or ""
    filename = None
    content_type = None
    msgtype = 'm.text'

    # Sticker
    if update.message.sticker:
        sticker = update.message.sticker
        file = await sticker.get_file()
        data = await file.download_as_bytearray()
        if sticker.is_animated:
            content_type = 'video/webm'
            filename = f"sticker_{update.message.message_id}.webm"
        else:
            content_type = 'image/webp'
            filename = f"sticker_{update.message.message_id}.webp"
        file_to_send = data
        msgtype = 'm.image' if not sticker.is_animated else 'm.video'
        caption = f"**{author_display}** enviou um sticker"

    # Foto
    elif update.message.photo:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        data = await file.download_as_bytearray()
        content_type = 'image/jpeg'
        filename = f"photo_{update.message.message_id}.jpg"
        file_to_send = data
        msgtype = 'm.image'
        caption = update.message.caption or ""

    # GIF (documento animado ou animação)
    elif update.message.document and update.message.document.mime_type == 'image/gif':
        doc = update.message.document
        file = await doc.get_file()
        data = await file.download_as_bytearray()
        content_type = 'image/gif'
        filename = doc.file_name or f"gif_{update.message.message_id}.gif"
        file_to_send = data
        msgtype = 'm.image'
        caption = update.message.caption or ""
    elif update.message.animation:
        anim = update.message.animation
        file = await anim.get_file()
        data = await file.download_as_bytearray()
        content_type = anim.mime_type or 'video/mp4'
        filename = anim.file_name or f"animation_{update.message.message_id}.mp4"
        file_to_send = data
        msgtype = 'm.video'
        caption = update.message.caption or ""

    # Vídeo
    elif update.message.video:
        video = update.message.video
        file = await video.get_file()
        data = await file.download_as_bytearray()
        content_type = video.mime_type or 'video/mp4'
        filename = video.file_name or f"video_{update.message.message_id}.mp4"
        file_to_send = data
        msgtype = 'm.video'
        caption = update.message.caption or ""

    # Nota de voz
    elif update.message.voice:
        voice = update.message.voice
        file = await voice.get_file()
        data = await file.download_as_bytearray()
        content_type = voice.mime_type or 'audio/ogg'
        filename = f"voice_{update.message.message_id}.ogg"
        file_to_send = data
        msgtype = 'm.audio'
        caption = update.message.caption or ""

    # Áudio (música)
    elif update.message.audio:
        audio = update.message.audio
        file = await audio.get_file()
        data = await file.download_as_bytearray()
        content_type = audio.mime_type or 'audio/mpeg'
        filename = audio.file_name or f"audio_{update.message.message_id}.mp3"
        file_to_send = data
        msgtype = 'm.audio'
        caption = update.message.caption or ""

    # Documento comum
    elif update.message.document:
        doc = update.message.document
        file = await doc.get_file()
        data = await file.download_as_bytearray()
        content_type = doc.mime_type or 'application/octet-stream'
        filename = doc.file_name or f"doc_{update.message.message_id}"
        file_to_send = data
        msgtype = 'm.file'
        caption = update.message.caption or ""

    # Se tem mídia
    if file_to_send:
        mxc_url = await upload_to_matrix(file_to_send, filename, content_type)
        if mxc_url:
            content = {
                "msgtype": msgtype,
                "body": caption or filename,
                "url": mxc_url,
                "info": {
                    "mimetype": content_type,
                    "size": len(file_to_send)
                }
            }
            if caption:
                content['body'] = caption
                # Adiciona formatação HTML para o Matrix
                content['format'] = "org.matrix.custom.html"
                content['formatted_body'] = markdown_to_html(caption)
            if reply_target_matrix:
                content['m.relates_to'] = {
                    'm.in_reply_to': {
                        'event_id': reply_target_matrix
                    }
                }

            try:
                await matrix_client.room_send(
                    room_id=bridge['matrix_room'],
                    message_type="m.room.message",
                    content=content
                )
                print(f"[Telegram -> Matrix] Mídia enviada: {filename}")
            except Exception as e:
                print(f"[Telegram -> Matrix] Erro: {e}")

            # Discord
            for channel_id in bridge.get('discord_channels', []):
                channel = discord_client.get_channel(channel_id)
                if channel:
                    discord_file = DiscordFile(fp=BytesIO(file_to_send), filename=filename)
                    kwargs = {}
                    if reply_target_discord:
                        kwargs['reference'] = MessageReference(message_id=reply_target_discord, channel_id=channel_id)
                    try:
                        # Para o Discord, usamos ** para negrito
                        discord_text = f"**{author_display}:** {caption}"
                        sent = await channel.send(content=discord_text, file=discord_file, **kwargs)
                        msg_map[str(sent.id)] = {'platform': 'telegram', 'chat_id': chat_id, 'message_id': update.message.message_id}
                        msg_map[str(update.message.message_id)] = {'platform': 'discord', 'channel_id': channel_id, 'message_id': sent.id}
                        save_message_map(msg_map)
                        print(f"[Telegram -> Discord {channel_id}] Mídia enviada")
                    except Exception as e:
                        print(f"[Telegram -> Discord {channel_id}] Erro: {e}")

    # Texto puro
    elif update.message.text:
        text = update.message.text
        text_to_send = f"**{author_display}:** {text}"

        # Matrix com formatação
        content = {
            "msgtype": "m.text",
            "body": text_to_send,
            "format": "org.matrix.custom.html",
            "formatted_body": f"<b>{escape_html(author_display)}:</b> {escape_html(text)}"
        }
        if reply_target_matrix:
            content['m.relates_to'] = {
                'm.in_reply_to': {
                    'event_id': reply_target_matrix
                }
            }

        try:
            await matrix_client.room_send(
                room_id=bridge['matrix_room'],
                message_type="m.room.message",
                content=content
            )
            print(f"[Telegram -> Matrix] OK")
        except Exception as e:
            print(f"[Telegram -> Matrix] Erro: {e}")

        # Discord
        for channel_id in bridge.get('discord_channels', []):
            channel = discord_client.get_channel(channel_id)
            if channel:
                kwargs = {}
                if reply_target_discord:
                    kwargs['reference'] = MessageReference(message_id=reply_target_discord, channel_id=channel_id)
                sent = await channel.send(text_to_send, **kwargs)
                msg_map[str(sent.id)] = {'platform': 'telegram', 'chat_id': chat_id, 'message_id': update.message.message_id}
                msg_map[str(update.message.message_id)] = {'platform': 'discord', 'channel_id': channel_id, 'message_id': sent.id}
                save_message_map(msg_map)
                print(f"[Telegram -> Discord {channel_id}] OK")
            else:
                print(f"[Telegram] Canal Discord {channel_id} não encontrado")

async def telegram_edit_callback(update: Update, context):
    if not update.edited_message:
        return
    msg = update.edited_message
    chat_id = update.effective_chat.id
    if chat_id not in telegram_to_bridge:
        return
    bridge = telegram_to_bridge[chat_id]

    msg_map = load_message_map()
    msg_id_str = str(msg.message_id)
    if msg_id_str not in msg_map:
        return

    target = msg_map[msg_id_str]
    user = msg.from_user
    author_display = user.full_name or user.first_name
    if target['platform'] == 'matrix':
        room_id = target['room_id']
        event_id = target['event_id']
        new_content = {
            "msgtype": "m.text",
            "body": f"**{author_display}:** {msg.text}",
            "format": "org.matrix.custom.html",
            "formatted_body": f"<b>{author_display}:</b> {msg.text}"
        }
        await send_matrix_edit(room_id, event_id, new_content)
        print(f"[Telegram -> Matrix] Mensagem editada: {msg.message_id}")
    elif target['platform'] == 'discord':
        channel_id = target['channel_id']
        message_id = target['message_id']
        new_text = f"**{author_display}:** {msg.text}"
        await send_discord_edit(channel_id, message_id, new_text)
        print(f"[Telegram -> Discord] Mensagem editada: {msg.message_id}")

# Registra handlers do Telegram
telegram_app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), telegram_message_callback))
telegram_app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, telegram_edit_callback))

# ---------------- CALLBACKS DE DELEÇÃO ----------------
# Adicionados depois que matrix_client estiver definido, dentro de main()

# ---------------- LOOP PRINCIPAL ----------------
async def main():
    global matrix_client
    matrix_client = await matrix_login()
    if not matrix_client:
        return

    # Registra callbacks Matrix para mensagens
    for event_class in [
        RoomMessageText, RoomMessageEmote, RoomMessageNotice,
        RoomMessageMedia, RoomMessageFile, RoomMessageImage,
        RoomMessageVideo, RoomMessageAudio
    ]:
        matrix_client.add_event_callback(matrix_message_callback, event_class)

    # Registra callback para redações
    matrix_client.add_event_callback(handle_matrix_redaction, RedactionEvent)

    # Entra nas salas
    for bridge in BRIDGES:
        room_id = bridge['matrix_room']
        try:
            await matrix_client.join(room_id)
            print(f"[Matrix] Entrou na sala: {room_id} ({bridge.get('name', 'sem nome')})")
        except Exception as e:
            print(f"[Matrix] Erro entrando na sala {room_id}: {e}")

    # Inicia Discord
    asyncio.create_task(discord_client.start(DISCORD_TOKEN))

    # Inicia Telegram
    await telegram_app.initialize()
    await telegram_app.updater.start_polling()
    await telegram_app.start()
    print("[Telegram] Polling iniciado")

    # Sincronização Matrix
    print("[Matrix] Iniciando sync_forever...")
    try:
        await matrix_client.sync_forever(timeout=30000)
    except KeyboardInterrupt:
        print("Interrompido")
    finally:
        await matrix_client.close()
        await telegram_app.stop()
        await telegram_app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
