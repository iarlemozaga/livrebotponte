import asyncio
import json
import os
import tempfile
import mimetypes
import re
import aiohttp
from pathlib import Path
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
TEMP_DIR = Path(tempfile.gettempdir()) / "matrix_bridge"
TEMP_DIR.mkdir(exist_ok=True)

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

# ---------------- PERSISTÊNCIA DE ESTADO (sync_token e timestamps) ----------------
def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {"sync_token": None, "last_ts": {}}

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

async def download_file(url, output_path):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                with open(output_path, 'wb') as f:
                    f.write(await resp.read())
                return True
    return False

async def upload_to_matrix(file_path, filename, content_type):
    """Faz upload de um arquivo do disco para o Matrix."""
    file_path = Path(file_path)
    if not file_path.exists():
        return None
    file_size = file_path.stat().st_size
    with open(file_path, 'rb') as f:
        resp = await matrix_client.upload(
            data_provider=f,
            content_type=content_type,
            filename=filename,
            filesize=file_size
        )
    if isinstance(resp, UploadResponse) and resp.content_uri:
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

async def download_matrix_file(client, url, output_path):
    """Baixa um arquivo do Matrix (mxc://) para um caminho local."""
    if url.startswith('mxc://'):
        parts = url[6:].split('/')
        server_name = parts[0]
        media_id = parts[1]
        # O terceiro argumento é o filename (caminho onde salvar)
        resp = await client.download(server_name, media_id, str(output_path))
        # Se não houver exceção, consideramos sucesso
        return True
    return False

def escape_html(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def markdown_to_html(text):
    """Converte **texto** para <b>texto</b>."""
    return re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)

# ---------------- FUNÇÕES DE EDIÇÃO ----------------
async def send_matrix_edit(room_id, event_id, new_content):
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
    channel = discord_client.get_channel(channel_id)
    if channel:
        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(content=new_text)
        except Exception as e:
            print(f"[Discord] Erro ao editar mensagem {message_id}: {e}")

async def send_telegram_edit(chat_id, message_id, new_text):
    try:
        await telegram_bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=new_text,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print(f"[Telegram] Erro ao editar mensagem {message_id}: {e}")

# ---------------- FUNÇÕES DE DELEÇÃO ----------------
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

    # Filtro por timestamp para evitar mensagens antigas (fallback caso sync_token falhe)
    ts = getattr(event, 'server_timestamp', 0)
    state = load_state()
    last_ts_dict = state.get('last_ts', {})
    last_ts = last_ts_dict.get(room.room_id, 0)
    if ts <= last_ts:
        print(f"[Matrix] Ignorando mensagem antiga (ts={ts}, last={last_ts})")
        return
    last_ts_dict[room.room_id] = ts
    state['last_ts'] = last_ts_dict
    save_state(state)

    content = getattr(event, 'source', {}).get('content', {})
    relates_to = content.get('m.relates_to', {})

    # Trata edição
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

    # Ignora outros relates (reações etc)
    if relates_to and 'rel_type' in relates_to:
        return

    sender_display = get_matrix_display_name(room, event.sender)

    reply_to_event_id = None
    if 'm.in_reply_to' in relates_to:
        reply_to_event_id = relates_to['m.in_reply_to'].get('event_id')

    text_to_send = None
    file_path = None
    filename = None
    content_type = None
    msgtype = None

    if isinstance(event, (RoomMessageText, RoomMessageEmote, RoomMessageNotice)):
        msgtype = getattr(event, 'msgtype', 'm.text')
        body = getattr(event, 'body', '')
        if msgtype == 'm.emote':
            text_to_send = f"* {sender_display} {body}"
        else:
            text_to_send = f"**{sender_display}:** {body}"
    elif isinstance(event, (RoomMessageImage, RoomMessageVideo, RoomMessageAudio, RoomMessageMedia)):
        url = getattr(event, 'url', None)
        body = getattr(event, 'body', '')
        if url:
            file_path = TEMP_DIR / f"matrix_{event.event_id}"
            success = await download_matrix_file(matrix_client, url, file_path)
            if success and file_path.exists():
                filename = getattr(event, 'body', 'media') or f"media_{event.event_id}"
                content_type = getattr(event, 'mimetype', 'application/octet-stream')
                if hasattr(event, 'msgtype'):
                    msgtype = event.msgtype
                else:
                    if content_type.startswith('image/'):
                        msgtype = 'm.image'
                    elif content_type.startswith('video/'):
                        msgtype = 'm.video'
                    elif content_type.startswith('audio/'):
                        msgtype = 'm.audio'
                    else:
                        msgtype = 'm.file'
                if body:
                    text_to_send = f"**{sender_display}:** {body}"
                else:
                    text_to_send = f"**{sender_display}** enviou um arquivo"
            else:
                text_to_send = f"**{sender_display}** enviou uma mídia (falha no download)"
                file_path = None
        else:
            text_to_send = f"**{sender_display}** enviou uma mídia sem URL"
    elif hasattr(event, 'msgtype') and event.msgtype == 'm.sticker':
        url = getattr(event, 'url', None)
        if url:
            file_path = TEMP_DIR / f"sticker_{event.event_id}"
            success = await download_matrix_file(matrix_client, url, file_path)
            if success and file_path.exists():
                filename = f"sticker_{event.event_id}"
                content_type = getattr(event, 'mimetype', 'image/png')
                msgtype = 'm.image'
                text_to_send = f"**{sender_display}** enviou um sticker"
            else:
                text_to_send = f"**{sender_display}** enviou um sticker (falha no download)"
                file_path = None
        else:
            text_to_send = f"**{sender_display}** enviou um sticker sem URL"

    if not text_to_send and not file_path:
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
                if file_path and file_path.exists():
                    with open(file_path, 'rb') as f:
                        discord_file = DiscordFile(f, filename=filename)
                        sent = await channel.send(content=text_to_send, file=discord_file, **kwargs)
                else:
                    sent = await channel.send(text_to_send, **kwargs)
                msg_map[event.event_id] = {'platform': 'discord', 'channel_id': channel_id, 'message_id': sent.id}
                msg_map[str(sent.id)] = {'platform': 'matrix', 'room_id': room.room_id, 'event_id': event.event_id}
                print(f"[Matrix -> Discord {channel_id}] OK (ts={ts})")
            except Exception as e:
                print(f"[Matrix -> Discord {channel_id}] Erro: {e}")

    # Envia para Telegram
    for chat_id in bridge.get('telegram_chats', []):
        try:
            kwargs = {}
            if reply_target_telegram:
                kwargs['reply_to_message_id'] = reply_target_telegram
            if file_path and file_path.exists():
                with open(file_path, 'rb') as f:
                    caption_html = markdown_to_html(text_to_send)
                    if msgtype == 'm.image':
                        sent = await telegram_bot.send_photo(
                            chat_id=chat_id,
                            photo=f,
                            caption=caption_html,
                            parse_mode=ParseMode.HTML,
                            **kwargs
                        )
                    elif msgtype == 'm.video':
                        sent = await telegram_bot.send_video(
                            chat_id=chat_id,
                            video=f,
                            caption=caption_html,
                            parse_mode=ParseMode.HTML,
                            **kwargs
                        )
                    elif msgtype == 'm.audio':
                        sent = await telegram_bot.send_audio(
                            chat_id=chat_id,
                            audio=f,
                            caption=caption_html,
                            parse_mode=ParseMode.HTML,
                            **kwargs
                        )
                    else:
                        sent = await telegram_bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            caption=caption_html,
                            filename=filename,
                            parse_mode=ParseMode.HTML,
                            **kwargs
                        )
            else:
                text_html = markdown_to_html(text_to_send)
                sent = await telegram_bot.send_message(
                    chat_id=chat_id,
                    text=text_html,
                    parse_mode=ParseMode.HTML,
                    **kwargs
                )
            msg_map[event.event_id] = {'platform': 'telegram', 'chat_id': chat_id, 'message_id': sent.message_id}
            msg_map[str(sent.message_id)] = {'platform': 'matrix', 'room_id': room.room_id, 'event_id': event.event_id}
            print(f"[Matrix -> Telegram {chat_id}] OK (ts={ts})")
        except Exception as e:
            print(f"[Matrix -> Telegram {chat_id}] Erro: {e}")

    # Limpa arquivo temporário
    if file_path and file_path.exists():
        file_path.unlink()

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
            file_ext = 'png'
            if sticker.format == discord.StickerFormatType.lottie:
                file_ext = 'json'
            elif sticker.format == discord.StickerFormatType.apng:
                file_ext = 'png'
            file_path = TEMP_DIR / f"sticker_{sticker.id}.{file_ext}"
            success = await download_file(sticker_url, file_path)
            if success:
                # Envia para Matrix
                mxc_url = await upload_to_matrix(file_path, sticker.name, 'image/png')
                if mxc_url:
                    content = {
                        "msgtype": "m.image",
                        "body": f"Sticker: {sticker.name}",
                        "url": mxc_url,
                        "info": {"mimetype": 'image/png', "size": file_path.stat().st_size}
                    }
                    if reply_target_matrix:
                        content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_target_matrix}}
                    try:
                        await matrix_client.room_send(
                            room_id=bridge['matrix_room'],
                            message_type="m.room.message",
                            content=content
                        )
                        print(f"[Discord -> Matrix] Sticker enviado: {sticker.name}")
                    except Exception as e:
                        print(f"[Discord -> Matrix] Erro ao enviar sticker: {e}")

                # Envia para Telegram
                for chat_id in bridge.get('telegram_chats', []):
                    try:
                        kwargs = {}
                        if reply_target_telegram:
                            kwargs['reply_to_message_id'] = reply_target_telegram
                        with open(file_path, 'rb') as f:
                            sent = await telegram_bot.send_document(
                                chat_id=chat_id,
                                document=f,
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

            if file_path.exists():
                file_path.unlink()

    # Processa attachments
    if message.attachments:
        for attachment in message.attachments:
            file_path = TEMP_DIR / f"discord_{attachment.id}_{attachment.filename}"
            success = await download_file(attachment.url, file_path)
            if success:
                # Matrix
                mxc_url = await upload_to_matrix(file_path, attachment.filename, attachment.content_type)
                if mxc_url:
                    msgtype = 'm.file'
                    if attachment.content_type.startswith('image/'):
                        msgtype = 'm.image'
                    elif attachment.content_type.startswith('video/'):
                        msgtype = 'm.video'
                    elif attachment.content_type.startswith('audio/'):
                        msgtype = 'm.audio'
                    content = {
                        "msgtype": msgtype,
                        "body": attachment.filename,
                        "url": mxc_url,
                        "info": {
                            "mimetype": attachment.content_type,
                            "size": file_path.stat().st_size
                        }
                    }
                    if message.content:
                        content['body'] = message.content
                        content['format'] = "org.matrix.custom.html"
                        content['formatted_body'] = f"<b>{author_display}:</b> {message.content}"
                    if reply_target_matrix:
                        content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_target_matrix}}
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
                        with open(file_path, 'rb') as f:
                            caption_html = f"<b>{escape_html(author_display)}</b>"
                            if message.content:
                                caption_html += f" {escape_html(message.content)}"
                            if attachment.content_type.startswith('image/'):
                                sent = await telegram_bot.send_photo(
                                    chat_id=chat_id,
                                    photo=f,
                                    caption=caption_html,
                                    parse_mode=ParseMode.HTML,
                                    **kwargs
                                )
                            elif attachment.content_type.startswith('video/'):
                                sent = await telegram_bot.send_video(
                                    chat_id=chat_id,
                                    video=f,
                                    caption=caption_html,
                                    parse_mode=ParseMode.HTML,
                                    **kwargs
                                )
                            elif attachment.content_type.startswith('audio/'):
                                sent = await telegram_bot.send_audio(
                                    chat_id=chat_id,
                                    audio=f,
                                    caption=caption_html,
                                    parse_mode=ParseMode.HTML,
                                    **kwargs
                                )
                            else:
                                sent = await telegram_bot.send_document(
                                    chat_id=chat_id,
                                    document=f,
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

            if file_path.exists():
                file_path.unlink()

    # Texto puro
    elif message.content:
        text_to_send = f"**{author_display}:** {message.content}"

        # Matrix
        content = {
            "msgtype": "m.text",
            "body": text_to_send,
            "format": "org.matrix.custom.html",
            "formatted_body": markdown_to_html(text_to_send)
        }
        if reply_target_matrix:
            content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_target_matrix}}

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
    file_path = None
    caption = update.message.caption or ""
    filename = None
    content_type = None
    msgtype = None

    # Sticker
    if update.message.sticker:
        sticker = update.message.sticker
        file = await sticker.get_file()
        if sticker.is_animated:
            ext = 'webm'
            content_type = 'video/webm'
            msgtype = 'm.video'
        else:
            ext = 'webp'
            content_type = 'image/webp'
            msgtype = 'm.image'
        filename = f"sticker_{update.message.message_id}.{ext}"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        caption = f"**{author_display}** enviou um sticker"

    # Foto
    elif update.message.photo:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        filename = f"photo_{update.message.message_id}.jpg"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = 'image/jpeg'
        msgtype = 'm.image'
        caption = update.message.caption or ""

    # GIF/Animação
    elif update.message.animation:
        anim = update.message.animation
        file = await anim.get_file()
        filename = anim.file_name or f"animation_{update.message.message_id}.mp4"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = anim.mime_type or 'video/mp4'
        msgtype = 'm.video'
        caption = update.message.caption or ""

    # Vídeo
    elif update.message.video:
        video = update.message.video
        file = await video.get_file()
        filename = video.file_name or f"video_{update.message.message_id}.mp4"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = video.mime_type or 'video/mp4'
        msgtype = 'm.video'
        caption = update.message.caption or ""

    # Voz
    elif update.message.voice:
        voice = update.message.voice
        file = await voice.get_file()
        filename = f"voice_{update.message.message_id}.ogg"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = voice.mime_type or 'audio/ogg'
        msgtype = 'm.audio'
        caption = update.message.caption or ""

    # Áudio
    elif update.message.audio:
        audio = update.message.audio
        file = await audio.get_file()
        filename = audio.file_name or f"audio_{update.message.message_id}.mp3"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = audio.mime_type or 'audio/mpeg'
        msgtype = 'm.audio'
        caption = update.message.caption or ""

    # Documento
    elif update.message.document:
        doc = update.message.document
        file = await doc.get_file()
        filename = doc.file_name or f"doc_{update.message.message_id}"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = doc.mime_type or 'application/octet-stream'
        msgtype = 'm.file'
        caption = update.message.caption or ""

    # Se tem mídia
    if file_path and file_path.exists():
        mxc_url = await upload_to_matrix(file_path, filename, content_type)
        if mxc_url:
            content = {
                "msgtype": msgtype,
                "body": caption or filename,
                "url": mxc_url,
                "info": {
                    "mimetype": content_type,
                    "size": file_path.stat().st_size
                }
            }
            if caption:
                content['body'] = caption
                content['format'] = "org.matrix.custom.html"
                content['formatted_body'] = markdown_to_html(caption)
            if reply_target_matrix:
                content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_target_matrix}}

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
                kwargs = {}
                if reply_target_discord:
                    kwargs['reference'] = MessageReference(message_id=reply_target_discord, channel_id=channel_id)
                try:
                    with open(file_path, 'rb') as f:
                        discord_file = DiscordFile(f, filename=filename)
                        sent = await channel.send(content=f"**{author_display}:** {caption}", file=discord_file, **kwargs)
                    msg_map[str(sent.id)] = {'platform': 'telegram', 'chat_id': chat_id, 'message_id': update.message.message_id}
                    msg_map[str(update.message.message_id)] = {'platform': 'discord', 'channel_id': channel_id, 'message_id': sent.id}
                    save_message_map(msg_map)
                    print(f"[Telegram -> Discord {channel_id}] Mídia enviada")
                except Exception as e:
                    print(f"[Telegram -> Discord {channel_id}] Erro: {e}")

        file_path.unlink()

    # Texto puro
    elif update.message.text:
        text = update.message.text
        text_to_send = f"**{author_display}:** {text}"

        # Matrix
        content = {
            "msgtype": "m.text",
            "body": text_to_send,
            "format": "org.matrix.custom.html",
            "formatted_body": markdown_to_html(text_to_send)
        }
        if reply_target_matrix:
            content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_target_matrix}}

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

telegram_app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), telegram_message_callback))
telegram_app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, telegram_edit_callback))

# ---------------- LOOP PRINCIPAL ----------------
async def main():
    global matrix_client
    matrix_client = await matrix_login()
    if not matrix_client:
        return

    # Carrega sync_token do estado
    state = load_state()
    sync_token = state.get('sync_token')

    # Registra callbacks Matrix para mensagens
    for event_class in [
        RoomMessageText, RoomMessageEmote, RoomMessageNotice,
        RoomMessageMedia, RoomMessageFile, RoomMessageImage,
        RoomMessageVideo, RoomMessageAudio
    ]:
        matrix_client.add_event_callback(matrix_message_callback, event_class)

    matrix_client.add_event_callback(handle_matrix_redaction, RedactionEvent)

    # Entra nas salas
    for bridge in BRIDGES:
        room_id = bridge['matrix_room']
        try:
            await matrix_client.join(room_id)
            print(f"[Matrix] Entrou na sala: {room_id} ({bridge.get('name', 'sem nome')})")
        except Exception as e:
            print(f"[Matrix] Erro entrando na sala {room_id}: {e}")

    # Inicia Discord em background
    asyncio.create_task(discord_client.start(DISCORD_TOKEN))

    # Inicia Telegram
    await telegram_app.initialize()
    await telegram_app.updater.start_polling()
    await telegram_app.start()
    print("[Telegram] Polling iniciado")

    # Sincronização Matrix
    print("[Matrix] Iniciando sync_forever...")
    try:
        await matrix_client.sync_forever(timeout=30000, since=sync_token)
    except KeyboardInterrupt:
        print("Interrompido")
    except Exception as e:
        print(f"Erro no sync_forever: {e}")
    finally:
        # Salva o next_batch atual antes de sair
        if matrix_client.next_batch:
            state['sync_token'] = matrix_client.next_batch
            save_state(state)
            print(f"[Matrix] Sync_token salvo: {matrix_client.next_batch[:10]}...")
        await matrix_client.close()
        # Para o Telegram corretamente
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
