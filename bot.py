import asyncio
import json
import os
import tempfile
import mimetypes
import re
import time
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

# ================== PERSISTÊNCIA OTIMIZADA ==================

def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {"sync_token": None, "last_ts": {}}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

def load_message_map():
    try:
        with open(MESSAGE_MAP_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_message_map(map_data):
    # Remove entradas antigas (mais de 30 dias) para não acumular
    now = time.time()
    to_delete = []
    for k, v in map_data.items():
        if 'ts' in v and now - v['ts'] > 2592000:  # 30 dias
            to_delete.append(k)
    for k in to_delete:
        del map_data[k]
    with open(MESSAGE_MAP_FILE, 'w') as f:
        json.dump(map_data, f)

# ================== FUNÇÕES DE REDE ROBUSTAS ==================

async def matrix_login():
    """Login com senha e retorno do client."""
    client = AsyncClient(MATRIX_HOMESERVER, MATRIX_USERNAME)
    try:
        resp = await client.login(MATRIX_PASSWORD)
        if hasattr(resp, 'access_token'):
            print(f"✅ [Matrix] Login OK: {resp.user_id}")
            return client
        else:
            print(f"❌ [Matrix] Falha login: {resp}")
            return None
    except Exception as e:
        print(f"❌ [Matrix] Exceção login: {e}")
        return None

async def download_file_http(url, output_path):
    """Download HTTP simples (Discord/Telegram)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as resp:
                if resp.status == 200:
                    with open(output_path, 'wb') as f:
                        f.write(await resp.read())
                    print(f"✅ [HTTP] Download OK: {output_path}")
                    return True
                else:
                    print(f"❌ [HTTP] Download falhou: status {resp.status}")
                    return False
    except Exception as e:
        print(f"❌ [HTTP] Exceção download: {e}")
        return False

async def download_matrix_file(client, url, output_path):
    """Baixa arquivo do Matrix (mxc://). Versão CORRIGIDA."""
    if not url.startswith('mxc://'):
        print(f"❌ [Matrix] download: URL inválida: {url}")
        return False
    parts = url[6:].split('/')
    server_name = parts[0]
    media_id = parts[1]
    print(f"📥 [Matrix] download: iniciando {media_id} de {server_name}")
    try:
        # A API correta: download(server_name, media_id, filename=...)
        resp = await client.download(server_name, media_id, filename=str(output_path))
        # Verifica se o arquivo foi criado
        if output_path.exists() and output_path.stat().st_size > 0:
            print(f"✅ [Matrix] download: arquivo salvo ({output_path.stat().st_size} bytes)")
            return True
        else:
            print(f"❌ [Matrix] download: arquivo não criado ou vazio")
            return False
    except Exception as e:
        print(f"❌ [Matrix] download: exceção: {e}")
        return False

async def upload_to_matrix(file_path, filename, content_type):
    """
    Upload de arquivo para o Matrix.
    Corrigido: passa filesize explicitamente e usa o arquivo corretamente.
    """
    if not matrix_client or not matrix_client.access_token:
        print("❌ [Matrix] upload: cliente desconectado")
        return None

    file_path = Path(file_path)
    if not file_path.exists() or file_path.stat().st_size == 0:
        print(f"❌ [Matrix] upload: arquivo inválido: {file_path}")
        return None

    file_size = file_path.stat().st_size
    print(f"📤 [Matrix] upload: {filename} ({file_size} bytes, {content_type})")

    try:
        with open(file_path, 'rb') as f:
            resp = await matrix_client.upload(
                data_provider=f,
                content_type=content_type,
                filename=filename,
                filesize=file_size  # ESSENCIAL para evitar erro 400
            )
        if isinstance(resp, UploadResponse) and resp.content_uri:
            print(f"✅ [Matrix] upload: sucesso, URI: {resp.content_uri}")
            return resp.content_uri
        else:
            print(f"❌ [Matrix] upload: resposta inesperada: {resp}")
            return None
    except Exception as e:
        print(f"❌ [Matrix] upload: exceção: {e}")
        return None

# ================== UTILITÁRIOS ==================

def get_matrix_display_name(room, user_id):
    if room and hasattr(room, 'users'):
        user_info = room.users.get(user_id)
        if user_info and user_info.display_name:
            return user_info.display_name
    return user_id.split(':')[0].lstrip('@')

def escape_html(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def markdown_to_html(text):
    return re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)

# ================== EDIÇÕES ==================

async def send_matrix_edit(room_id, event_id, new_content):
    if not matrix_client or not matrix_client.access_token:
        return
    content = {
        "msgtype": "m.text",
        "body": f" * {new_content['body']}",
        "m.new_content": new_content,
        "m.relates_to": {"rel_type": "m.replace", "event_id": event_id}
    }
    try:
        await matrix_client.room_send(room_id, "m.room.message", content)
        print(f"✏️ [Matrix] Edição enviada para {event_id}")
    except Exception as e:
        print(f"❌ [Matrix] Erro ao editar: {e}")

async def send_discord_edit(channel_id, message_id, new_text):
    channel = discord_client.get_channel(channel_id)
    if channel:
        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(content=new_text)
            print(f"✏️ [Discord] Mensagem {message_id} editada")
        except Exception as e:
            print(f"❌ [Discord] Erro ao editar: {e}")

async def send_telegram_edit(chat_id, message_id, new_text):
    try:
        await telegram_bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=new_text, parse_mode=ParseMode.HTML
        )
        print(f"✏️ [Telegram] Mensagem {message_id} editada")
    except Exception as e:
        print(f"❌ [Telegram] Erro ao editar: {e}")

# ================== DELEÇÕES (CROSS-DELETING) ==================

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
        ch = discord_client.get_channel(target['channel_id'])
        if ch:
            try:
                msg = await ch.fetch_message(target['message_id'])
                await msg.delete()
                print(f"🗑️ [Matrix -> Discord] Mensagem {target['message_id']} apagada")
                del msg_map[redacts]
                if str(target['message_id']) in msg_map:
                    del msg_map[str(target['message_id'])]
                save_message_map(msg_map)
            except Exception as e:
                print(f"❌ [Matrix -> Discord] Erro ao apagar: {e}")
    elif target['platform'] == 'telegram':
        try:
            await telegram_bot.delete_message(chat_id=target['chat_id'], message_id=target['message_id'])
            print(f"🗑️ [Matrix -> Telegram] Mensagem {target['message_id']} apagada")
            del msg_map[redacts]
            if str(target['message_id']) in msg_map:
                del msg_map[str(target['message_id'])]
            save_message_map(msg_map)
        except Exception as e:
            print(f"❌ [Matrix -> Telegram] Erro ao apagar: {e}")

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
        if matrix_client and matrix_client.access_token:
            try:
                await matrix_client.room_redact(target['room_id'], target['event_id'])
                print(f"🗑️ [Discord -> Matrix] Mensagem {target['event_id']} redacionada")
                del msg_map[msg_id_str]
                if target['event_id'] in msg_map:
                    del msg_map[target['event_id']]
                save_message_map(msg_map)
            except Exception as e:
                print(f"❌ [Discord -> Matrix] Erro ao redacionar: {e}")
    elif target['platform'] == 'telegram':
        try:
            await telegram_bot.delete_message(chat_id=target['chat_id'], message_id=target['message_id'])
            print(f"🗑️ [Discord -> Telegram] Mensagem {target['message_id']} apagada")
            del msg_map[msg_id_str]
            if str(target['message_id']) in msg_map:
                del msg_map[str(target['message_id'])]
            save_message_map(msg_map)
        except Exception as e:
            print(f"❌ [Discord -> Telegram] Erro ao apagar: {e}")

# ================== CALLBACK MATRIX ==================

async def matrix_message_callback(room: MatrixRoom, event: RoomMessage):
    # Filtros iniciais
    if event.sender == matrix_client.user_id:
        return
    if room.room_id not in matrix_to_bridge:
        return
    bridge = matrix_to_bridge[room.room_id]

    # Ignora mensagens antigas via timestamp
    ts = getattr(event, 'server_timestamp', 0)
    state = load_state()
    last_ts = state.get('last_ts', {}).get(room.room_id, 0)
    if ts <= last_ts:
        print(f"⏭️ [Matrix] Mensagem antiga ignorada (ts={ts})")
        return
    state['last_ts'][room.room_id] = ts
    save_state(state)

    content = getattr(event, 'source', {}).get('content', {})
    relates_to = content.get('m.relates_to', {})

    # Se for edição, repassa
    if relates_to.get('rel_type') == 'm.replace':
        original = relates_to.get('event_id')
        new_body = content.get('m.new_content', {}).get('body', '')
        msg_map = load_message_map()
        if original in msg_map:
            target = msg_map[original]
            sender = get_matrix_display_name(room, event.sender)
            print(f"✏️ [Matrix] Edição recebida para {original}")
            if target['platform'] == 'discord':
                await send_discord_edit(target['channel_id'], target['message_id'], f"**{sender}:** {new_body}")
            elif target['platform'] == 'telegram':
                await send_telegram_edit(target['chat_id'], target['message_id'], f"<b>{escape_html(sender)}:</b> {escape_html(new_body)}")
        return

    # Se não for mensagem, ignora
    if not isinstance(event, (RoomMessageText, RoomMessageEmote, RoomMessageNotice,
                              RoomMessageImage, RoomMessageVideo, RoomMessageAudio,
                              RoomMessageMedia, RoomMessageFile)):
        return

    sender_display = get_matrix_display_name(room, event.sender)

    # Processa reply original
    reply_to_event_id = None
    if 'm.in_reply_to' in relates_to:
        reply_to_event_id = relates_to['m.in_reply_to'].get('event_id')

    # Baixa mídia se houver
    file_path = None
    filename = None
    content_type = None
    msgtype = None
    body_text = getattr(event, 'body', '')

    if isinstance(event, (RoomMessageImage, RoomMessageVideo, RoomMessageAudio, RoomMessageMedia, RoomMessageFile)):
        url = getattr(event, 'url', None)
        if url:
            file_path = TEMP_DIR / f"matrix_{event.event_id}"
            print(f"📥 [Matrix] Recebida mídia, baixando...")
            if await download_matrix_file(matrix_client, url, file_path):
                filename = getattr(event, 'body', 'media') or f"media_{event.event_id}"
                content_type = getattr(event, 'mimetype', 'application/octet-stream')
                if content_type.startswith('image/'):
                    msgtype = 'm.image'
                elif content_type.startswith('video/'):
                    msgtype = 'm.video'
                elif content_type.startswith('audio/'):
                    msgtype = 'm.audio'
                else:
                    msgtype = 'm.file'
                if body_text:
                    text_to_send = f"**{sender_display}:** {body_text}"
                else:
                    text_to_send = f"**{sender_display}** enviou um arquivo"
            else:
                text_to_send = f"**{sender_display}** enviou mídia (falha download)"
                file_path = None
        else:
            text_to_send = f"**{sender_display}** enviou mídia sem URL"
    else:
        # Mensagem de texto
        text_to_send = f"**{sender_display}:** {body_text}"
        if isinstance(event, RoomMessageEmote):
            text_to_send = f"* {sender_display} {body_text}"

    if 'text_to_send' not in locals() and not file_path:
        return

    msg_map = load_message_map()

    # Busca reply alvo
    reply_target_discord = None
    reply_target_telegram = None
    if reply_to_event_id and reply_to_event_id in msg_map:
        tinfo = msg_map[reply_to_event_id]
        if tinfo['platform'] == 'discord':
            reply_target_discord = tinfo['message_id']
        elif tinfo['platform'] == 'telegram':
            reply_target_telegram = tinfo['message_id']

    # Envia para Discord
    for ch_id in bridge.get('discord_channels', []):
        ch = discord_client.get_channel(ch_id)
        if ch:
            try:
                kwargs = {}
                if reply_target_discord:
                    kwargs['reference'] = MessageReference(message_id=reply_target_discord, channel_id=ch_id)
                if file_path and file_path.exists():
                    with open(file_path, 'rb') as f:
                        discord_file = DiscordFile(f, filename=filename)
                        sent = await ch.send(content=text_to_send, file=discord_file, **kwargs)
                else:
                    sent = await ch.send(text_to_send, **kwargs)
                msg_map[event.event_id] = {'platform': 'discord', 'channel_id': ch_id, 'message_id': sent.id, 'ts': time.time()}
                msg_map[str(sent.id)] = {'platform': 'matrix', 'room_id': room.room_id, 'event_id': event.event_id, 'ts': time.time()}
                print(f"✅ [Matrix -> Discord] Mensagem {sent.id} enviada")
            except Exception as e:
                print(f"❌ [Matrix->Discord] Erro: {e}")

    # Envia para Telegram
    for tg_id in bridge.get('telegram_chats', []):
        try:
            kwargs = {}
            if reply_target_telegram:
                kwargs['reply_to_message_id'] = reply_target_telegram
            if file_path and file_path.exists():
                with open(file_path, 'rb') as f:
                    if msgtype == 'm.image':
                        sent = await telegram_bot.send_photo(chat_id=tg_id, photo=f, caption=markdown_to_html(text_to_send), parse_mode=ParseMode.HTML, **kwargs)
                    elif msgtype == 'm.video':
                        sent = await telegram_bot.send_video(chat_id=tg_id, video=f, caption=markdown_to_html(text_to_send), parse_mode=ParseMode.HTML, **kwargs)
                    elif msgtype == 'm.audio':
                        sent = await telegram_bot.send_audio(chat_id=tg_id, audio=f, caption=markdown_to_html(text_to_send), parse_mode=ParseMode.HTML, **kwargs)
                    else:
                        sent = await telegram_bot.send_document(chat_id=tg_id, document=f, caption=markdown_to_html(text_to_send), filename=filename, parse_mode=ParseMode.HTML, **kwargs)
            else:
                sent = await telegram_bot.send_message(chat_id=tg_id, text=markdown_to_html(text_to_send), parse_mode=ParseMode.HTML, **kwargs)
            msg_map[event.event_id] = {'platform': 'telegram', 'chat_id': tg_id, 'message_id': sent.message_id, 'ts': time.time()}
            msg_map[str(sent.message_id)] = {'platform': 'matrix', 'room_id': room.room_id, 'event_id': event.event_id, 'ts': time.time()}
            print(f"✅ [Matrix -> Telegram] Mensagem {sent.message_id} enviada")
        except Exception as e:
            print(f"❌ [Matrix->Telegram] Erro: {e}")

    # Limpeza
    if file_path and file_path.exists():
        file_path.unlink()

    save_message_map(msg_map)

# ================== CALLBACK DISCORD ==================

@discord_client.event
async def on_ready():
    print(f"✅ [Discord] Conectado: {discord_client.user}")

@discord_client.event
async def on_message(message):
    if message.author.id == discord_client.user.id:
        return
    if message.channel.id not in discord_to_bridge:
        return
    bridge = discord_to_bridge[message.channel.id]

    author = message.author.display_name

    # Reply
    reply_to_msg_id = None
    if message.reference and message.reference.resolved:
        if isinstance(message.reference.resolved, discord.Message):
            reply_to_msg_id = message.reference.resolved.id

    msg_map = load_message_map()
    reply_target_matrix = None
    reply_target_telegram = None
    if reply_to_msg_id and str(reply_to_msg_id) in msg_map:
        tinfo = msg_map[str(reply_to_msg_id)]
        if tinfo['platform'] == 'matrix':
            reply_target_matrix = tinfo['event_id']
        elif tinfo['platform'] == 'telegram':
            reply_target_telegram = tinfo['message_id']

    # Stickers
    if message.stickers:
        for sticker in message.stickers:
            ext = 'png'
            if sticker.format == discord.StickerFormatType.lottie:
                ext = 'json'
            fpath = TEMP_DIR / f"sticker_{sticker.id}.{ext}"
            print(f"📥 [Discord] Baixando sticker {sticker.id}")
            if await download_file_http(sticker.url, fpath):
                # Matrix
                if matrix_client and matrix_client.access_token:
                    mxc = await upload_to_matrix(fpath, sticker.name, 'image/png')
                    if mxc:
                        content = {
                            "msgtype": "m.image",
                            "body": f"Sticker: {sticker.name}",
                            "url": mxc,
                            "info": {"mimetype": 'image/png', "size": fpath.stat().st_size}
                        }
                        if reply_target_matrix:
                            content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_target_matrix}}
                        try:
                            await matrix_client.room_send(bridge['matrix_room'], "m.room.message", content)
                            print(f"✅ [Discord -> Matrix] Sticker enviado")
                        except Exception as e:
                            print(f"❌ [Discord -> Matrix] Erro sticker: {e}")
                # Telegram
                for tg_id in bridge.get('telegram_chats', []):
                    try:
                        with open(fpath, 'rb') as f:
                            sent = await telegram_bot.send_document(
                                chat_id=tg_id, document=f,
                                caption=f"<b>{escape_html(author)}</b> enviou sticker",
                                parse_mode=ParseMode.HTML,
                                reply_to_message_id=reply_target_telegram
                            )
                        msg_map[str(sent.message_id)] = {'platform': 'discord', 'channel_id': message.channel.id, 'message_id': message.id, 'ts': time.time()}
                        msg_map[str(message.id)] = {'platform': 'telegram', 'chat_id': tg_id, 'message_id': sent.message_id, 'ts': time.time()}
                        print(f"✅ [Discord -> Telegram] Sticker enviado")
                    except Exception as e:
                        print(f"❌ [Discord -> Telegram] Erro sticker: {e}")
                fpath.unlink()
        save_message_map(msg_map)

    # Attachments
    elif message.attachments:
        for att in message.attachments:
            fpath = TEMP_DIR / f"discord_{att.id}_{att.filename}"
            print(f"📥 [Discord] Baixando attachment {att.filename}")
            if await download_file_http(att.url, fpath):
                # Matrix
                if matrix_client and matrix_client.access_token:
                    mxc = await upload_to_matrix(fpath, att.filename, att.content_type)
                    if mxc:
                        msgtype = 'm.file'
                        if att.content_type.startswith('image/'):
                            msgtype = 'm.image'
                        elif att.content_type.startswith('video/'):
                            msgtype = 'm.video'
                        elif att.content_type.startswith('audio/'):
                            msgtype = 'm.audio'
                        content = {
                            "msgtype": msgtype,
                            "body": att.filename,
                            "url": mxc,
                            "info": {"mimetype": att.content_type, "size": fpath.stat().st_size}
                        }
                        if message.content:
                            content['body'] = message.content
                            content['format'] = "org.matrix.custom.html"
                            content['formatted_body'] = f"<b>{author}:</b> {message.content}"
                        if reply_target_matrix:
                            content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_target_matrix}}
                        try:
                            await matrix_client.room_send(bridge['matrix_room'], "m.room.message", content)
                            print(f"✅ [Discord -> Matrix] Mídia enviada")
                        except Exception as e:
                            print(f"❌ [Discord -> Matrix] Erro mídia: {e}")
                # Telegram
                for tg_id in bridge.get('telegram_chats', []):
                    try:
                        with open(fpath, 'rb') as f:
                            caption = f"<b>{escape_html(author)}</b>"
                            if message.content:
                                caption += f" {escape_html(message.content)}"
                            if att.content_type.startswith('image/'):
                                sent = await telegram_bot.send_photo(
                                    chat_id=tg_id, photo=f, caption=caption,
                                    parse_mode=ParseMode.HTML, reply_to_message_id=reply_target_telegram
                                )
                            elif att.content_type.startswith('video/'):
                                sent = await telegram_bot.send_video(
                                    chat_id=tg_id, video=f, caption=caption,
                                    parse_mode=ParseMode.HTML, reply_to_message_id=reply_target_telegram
                                )
                            elif att.content_type.startswith('audio/'):
                                sent = await telegram_bot.send_audio(
                                    chat_id=tg_id, audio=f, caption=caption,
                                    parse_mode=ParseMode.HTML, reply_to_message_id=reply_target_telegram
                                )
                            else:
                                sent = await telegram_bot.send_document(
                                    chat_id=tg_id, document=f, caption=caption, filename=att.filename,
                                    parse_mode=ParseMode.HTML, reply_to_message_id=reply_target_telegram
                                )
                        msg_map[str(sent.message_id)] = {'platform': 'discord', 'channel_id': message.channel.id, 'message_id': message.id, 'ts': time.time()}
                        msg_map[str(message.id)] = {'platform': 'telegram', 'chat_id': tg_id, 'message_id': sent.message_id, 'ts': time.time()}
                        print(f"✅ [Discord -> Telegram] Mídia enviada")
                    except Exception as e:
                        print(f"❌ [Discord -> Telegram] Erro mídia: {e}")
                fpath.unlink()
        save_message_map(msg_map)

    # Texto puro
    elif message.content:
        text = f"**{author}:** {message.content}"
        # Matrix
        if matrix_client and matrix_client.access_token:
            content = {
                "msgtype": "m.text",
                "body": text,
                "format": "org.matrix.custom.html",
                "formatted_body": markdown_to_html(text)
            }
            if reply_target_matrix:
                content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_target_matrix}}
            try:
                await matrix_client.room_send(bridge['matrix_room'], "m.room.message", content)
                print(f"✅ [Discord -> Matrix] Texto enviado")
            except Exception as e:
                print(f"❌ [Discord -> Matrix] Erro texto: {e}")
        # Telegram
        for tg_id in bridge.get('telegram_chats', []):
            try:
                sent = await telegram_bot.send_message(
                    chat_id=tg_id,
                    text=f"<b>{escape_html(author)}:</b> {escape_html(message.content)}",
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=reply_target_telegram
                )
                msg_map[str(sent.message_id)] = {'platform': 'discord', 'channel_id': message.channel.id, 'message_id': message.id, 'ts': time.time()}
                msg_map[str(message.id)] = {'platform': 'telegram', 'chat_id': tg_id, 'message_id': sent.message_id, 'ts': time.time()}
                print(f"✅ [Discord -> Telegram] Texto enviado")
            except Exception as e:
                print(f"❌ [Discord -> Telegram] Erro texto: {e}")
        save_message_map(msg_map)

@discord_client.event
async def on_message_edit(before, after):
    if after.author.id == discord_client.user.id:
        return
    if before.content == after.content:
        return
    if after.channel.id not in discord_to_bridge:
        return

    msg_map = load_message_map()
    mid = str(after.id)
    if mid not in msg_map:
        return
    target = msg_map[mid]

    if target['platform'] == 'matrix' and matrix_client and matrix_client.access_token:
        new_content = {
            "msgtype": "m.text",
            "body": f"**{after.author.display_name}:** {after.content}",
            "format": "org.matrix.custom.html",
            "formatted_body": f"<b>{after.author.display_name}:</b> {after.content}"
        }
        await send_matrix_edit(target['room_id'], target['event_id'], new_content)
    elif target['platform'] == 'telegram':
        await send_telegram_edit(target['chat_id'], target['message_id'], f"<b>{escape_html(after.author.display_name)}:</b> {escape_html(after.content)}")

# ================== CALLBACK TELEGRAM ==================

async def telegram_message_callback(update: Update, context):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if chat_id not in telegram_to_bridge:
        return
    bridge = telegram_to_bridge[chat_id]

    user = update.effective_user
    author = user.full_name or user.first_name

    reply_id = None
    if update.message.reply_to_message:
        reply_id = update.message.reply_to_message.message_id

    msg_map = load_message_map()
    reply_target_matrix = None
    reply_target_discord = None
    if reply_id and str(reply_id) in msg_map:
        tinfo = msg_map[str(reply_id)]
        if tinfo['platform'] == 'matrix':
            reply_target_matrix = tinfo['event_id']
        elif tinfo['platform'] == 'discord':
            reply_target_discord = tinfo['message_id']

    # --- Processa mídia ---
    file_path = None
    caption = update.message.caption or ""
    filename = None
    content_type = None
    msgtype = None

    if update.message.sticker:
        s = update.message.sticker
        file = await s.get_file()
        ext = 'webm' if s.is_animated else 'webp'
        content_type = 'video/webm' if s.is_animated else 'image/webp'
        msgtype = 'm.video' if s.is_animated else 'm.image'
        filename = f"sticker_{update.message.message_id}.{ext}"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        print(f"📥 [Telegram] Sticker baixado: {filename}")
        caption = f"**{author}** enviou sticker"

    elif update.message.photo:
        p = update.message.photo[-1]
        file = await p.get_file()
        filename = f"photo_{update.message.message_id}.jpg"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = 'image/jpeg'
        msgtype = 'm.image'
        print(f"📥 [Telegram] Foto baixada: {filename}")
        caption = update.message.caption or ""

    elif update.message.animation:  # GIF
        a = update.message.animation
        file = await a.get_file()
        filename = a.file_name or f"anim_{update.message.message_id}.mp4"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = a.mime_type or 'video/mp4'
        msgtype = 'm.video'
        print(f"📥 [Telegram] GIF/Animação baixada: {filename}")
        caption = update.message.caption or ""

    elif update.message.video:
        v = update.message.video
        file = await v.get_file()
        filename = v.file_name or f"video_{update.message.message_id}.mp4"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = v.mime_type or 'video/mp4'
        msgtype = 'm.video'
        print(f"📥 [Telegram] Vídeo baixado: {filename}")
        caption = update.message.caption or ""

    elif update.message.voice:
        v = update.message.voice
        file = await v.get_file()
        filename = f"voice_{update.message.message_id}.ogg"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = v.mime_type or 'audio/ogg'
        msgtype = 'm.audio'
        print(f"📥 [Telegram] Voz baixada: {filename}")
        caption = update.message.caption or ""

    elif update.message.audio:
        a = update.message.audio
        file = await a.get_file()
        filename = a.file_name or f"audio_{update.message.message_id}.mp3"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = a.mime_type or 'audio/mpeg'
        msgtype = 'm.audio'
        print(f"📥 [Telegram] Áudio baixado: {filename}")
        caption = update.message.caption or ""

    elif update.message.document:
        d = update.message.document
        file = await d.get_file()
        filename = d.file_name or f"doc_{update.message.message_id}"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = d.mime_type or 'application/octet-stream'
        msgtype = 'm.file'
        print(f"📥 [Telegram] Documento baixado: {filename}")
        caption = update.message.caption or ""

    if file_path and file_path.exists():
        # Matrix
        if matrix_client and matrix_client.access_token:
            mxc = await upload_to_matrix(file_path, filename, content_type)
            if mxc:
                content = {
                    "msgtype": msgtype,
                    "body": caption or filename,
                    "url": mxc,
                    "info": {"mimetype": content_type, "size": file_path.stat().st_size}
                }
                if caption:
                    content['body'] = caption
                    content['format'] = "org.matrix.custom.html"
                    content['formatted_body'] = markdown_to_html(caption)
                if reply_target_matrix:
                    content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_target_matrix}}
                try:
                    await matrix_client.room_send(bridge['matrix_room'], "m.room.message", content)
                    print(f"✅ [Telegram -> Matrix] Mídia enviada")
                except Exception as e:
                    print(f"❌ [Telegram->Matrix] Erro envio: {e}")

        # Discord
        for ch_id in bridge.get('discord_channels', []):
            ch = discord_client.get_channel(ch_id)
            if ch:
                try:
                    with open(file_path, 'rb') as f:
                        discord_file = DiscordFile(f, filename=filename)
                        kwargs = {}
                        if reply_target_discord:
                            kwargs['reference'] = MessageReference(message_id=reply_target_discord, channel_id=ch_id)
                        sent = await ch.send(content=f"**{author}:** {caption}", file=discord_file, **kwargs)
                        msg_map[str(sent.id)] = {'platform': 'telegram', 'chat_id': chat_id, 'message_id': update.message.message_id, 'ts': time.time()}
                        msg_map[str(update.message.message_id)] = {'platform': 'discord', 'channel_id': ch_id, 'message_id': sent.id, 'ts': time.time()}
                        print(f"✅ [Telegram -> Discord] Mídia enviada")
                except Exception as e:
                    print(f"❌ [Telegram->Discord] Erro: {e}")
        file_path.unlink()

    # Texto puro
    elif update.message.text:
        text = f"**{author}:** {update.message.text}"
        # Matrix
        if matrix_client and matrix_client.access_token:
            content = {
                "msgtype": "m.text",
                "body": text,
                "format": "org.matrix.custom.html",
                "formatted_body": markdown_to_html(text)
            }
            if reply_target_matrix:
                content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_target_matrix}}
            try:
                await matrix_client.room_send(bridge['matrix_room'], "m.room.message", content)
                print(f"✅ [Telegram -> Matrix] Texto enviado")
            except Exception as e:
                print(f"❌ [Telegram->Matrix] Erro texto: {e}")
        # Discord
        for ch_id in bridge.get('discord_channels', []):
            ch = discord_client.get_channel(ch_id)
            if ch:
                kwargs = {}
                if reply_target_discord:
                    kwargs['reference'] = MessageReference(message_id=reply_target_discord, channel_id=ch_id)
                sent = await ch.send(text, **kwargs)
                msg_map[str(sent.id)] = {'platform': 'telegram', 'chat_id': chat_id, 'message_id': update.message.message_id, 'ts': time.time()}
                msg_map[str(update.message.message_id)] = {'platform': 'discord', 'channel_id': ch_id, 'message_id': sent.id, 'ts': time.time()}
                print(f"✅ [Telegram -> Discord] Texto enviado")

    save_message_map(msg_map)

async def telegram_edit_callback(update: Update, context):
    if not update.edited_message:
        return
    msg = update.edited_message
    chat_id = update.effective_chat.id
    if chat_id not in telegram_to_bridge:
        return
    msg_map = load_message_map()
    mid = str(msg.message_id)
    if mid not in msg_map:
        return
    target = msg_map[mid]
    user = msg.from_user
    author = user.full_name or user.first_name

    if target['platform'] == 'matrix' and matrix_client and matrix_client.access_token:
        new_content = {
            "msgtype": "m.text",
            "body": f"**{author}:** {msg.text}",
            "format": "org.matrix.custom.html",
            "formatted_body": f"<b>{author}:</b> {msg.text}"
        }
        await send_matrix_edit(target['room_id'], target['event_id'], new_content)
    elif target['platform'] == 'discord':
        await send_discord_edit(target['channel_id'], target['message_id'], f"**{author}:** {msg.text}")

telegram_app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), telegram_message_callback))
telegram_app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, telegram_edit_callback))

# ================== LOOP DE RECONEXÃO MATRIX ==================

async def run_matrix_sync():
    global matrix_client
    while True:
        try:
            matrix_client = await matrix_login()
            if not matrix_client:
                print("⏳ [Matrix] Aguardando 60s para reconectar...")
                await asyncio.sleep(60)
                continue

            # Registra callbacks
            for ec in [RoomMessageText, RoomMessageEmote, RoomMessageNotice,
                       RoomMessageMedia, RoomMessageFile, RoomMessageImage,
                       RoomMessageVideo, RoomMessageAudio]:
                matrix_client.add_event_callback(matrix_message_callback, ec)
            matrix_client.add_event_callback(handle_matrix_redaction, RedactionEvent)

            # Entra nas salas
            for bridge in BRIDGES:
                try:
                    await matrix_client.join(bridge['matrix_room'])
                    print(f"✅ [Matrix] Entrou na sala: {bridge.get('name', bridge['matrix_room'])}")
                except Exception as e:
                    print(f"❌ [Matrix] Erro ao entrar na sala {bridge['matrix_room']}: {e}")

            state = load_state()
            sync_token = state.get('sync_token')
            print("🔄 [Matrix] Iniciando sync_forever...")
            await matrix_client.sync_forever(timeout=30000, since=sync_token)

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"❌ [Matrix] Erro no sync: {e}. Reconectando em 60s...")
            if matrix_client:
                await matrix_client.close()
                matrix_client = None
            await asyncio.sleep(60)

# ================== MAIN ==================

async def main():
    # Inicia Discord
    discord_task = asyncio.create_task(discord_client.start(DISCORD_TOKEN))

    # Inicia Telegram
    await telegram_app.initialize()
    await telegram_app.updater.start_polling()
    await telegram_app.start()
    print("✅ [Telegram] OK")

    # Inicia Matrix
    matrix_task = asyncio.create_task(run_matrix_sync())

    try:
        await asyncio.gather(discord_task, matrix_task)
    except KeyboardInterrupt:
        print("🛑 Interrompido pelo usuário")
    finally:
        if matrix_client and matrix_client.next_batch:
            state = load_state()
            state['sync_token'] = matrix_client.next_batch
            save_state(state)
            print("💾 [Matrix] Sync_token salvo")
        if matrix_client:
            await matrix_client.close()
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
