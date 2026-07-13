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
import slixmpp
from slixmpp.exceptions import IqError, IqTimeout

# Carrega configurações
with open('config.json', 'r') as f:
    config = json.load(f)

# ---------------- CONFIGURAÇÕES ----------------
MATRIX_HOMESERVER = config['matrix']['homeserver']
MATRIX_USERNAME = config['matrix']['username']
MATRIX_PASSWORD = config['matrix']['password']

DISCORD_TOKEN = config['discord']['token']
DISCORD_WEBHOOK_NAME = config['discord'].get('webhook_name', '🌉 Bridge Bot')
USE_DISCORD_WEBHOOK = config['discord'].get('use_webhook', True)

TELEGRAM_TOKEN = config['telegram']['token']

XMPP_JID = config['xmpp']['jid']
XMPP_PASSWORD = config['xmpp']['password']
XMPP_SERVER = config['xmpp'].get('server', 'jabber.org')
XMPP_PORT = config['xmpp'].get('port', 5222)

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
xmpp_client = None

# Armazena webhooks do Discord por channel_id
discord_webhooks = {}

# ---------------- MAPEAMENTOS DE BRIDGES ----------------
matrix_to_bridge = {}
discord_to_bridge = {}
telegram_to_bridge = {}
xmpp_to_bridge = {}

for bridge in BRIDGES:
    room_id = bridge.get('matrix_room')

    if room_id:
        matrix_to_bridge[room_id] = bridge

    for ch in bridge.get('discord_channels', []):
        discord_to_bridge[ch] = bridge

    for tg in bridge.get('telegram_chats', []):
        telegram_to_bridge[tg] = bridge

    for xmpp_room in bridge.get('xmpp_rooms', []):
        xmpp_to_bridge[xmpp_room] = bridge

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
    """Baixa arquivo do Matrix e garante que a extensão correta seja aplicada, ignorando .bin."""
    if not url.startswith('mxc://'):
        print(f"❌ [Matrix] download: URL inválida: {url}")
        return None

    parts = url[6:].split('/')
    server_name = parts[0]
    media_id = parts[1]

    base_url = client.homeserver.rstrip('/')
    headers = {"Authorization": f"Bearer {client.access_token}"}

    print(f"📥 [Matrix] download: tentando HTTP Autenticado ({server_name}/{media_id})")

    try:
        async with aiohttp.ClientSession() as session:
            v1_url = f"{base_url}/_matrix/client/v1/media/download/{server_name}/{media_id}"
            resp = await session.get(v1_url, headers=headers)

            if resp.status != 200:
                v3_url = f"{base_url}/_matrix/media/v3/download/{server_name}/{media_id}"
                resp = await session.get(v3_url, headers=headers)

            if resp.status == 200:
                content_type = resp.headers.get('Content-Type', '')
                ext = mimetypes.guess_extension(content_type)

                # Força a correção de extensões problemáticas ou genéricas
                if ext == '.jpe':
                    ext = '.jpg'
                if not ext or ext == '.bin':
                    if 'image/jpeg' in content_type: ext = '.jpg'
                    elif 'image/png' in content_type: ext = '.png'
                    elif 'image/gif' in content_type: ext = '.gif'
                    elif 'image/webp' in content_type: ext = '.webp'
                    elif 'video/mp4' in content_type: ext = '.mp4'
                    elif 'audio/ogg' in content_type: ext = '.ogg'
                    else: ext = ''

                if ext and not output_path.endswith(ext):
                    final_path = f"{output_path}{ext}"
                else:
                    final_path = output_path

                with open(final_path, 'wb') as f:
                    f.write(await resp.read())

                print(f"✅ [Matrix] download concluído (Salvo como: {final_path})")
                return final_path
            else:
                print(f"❌ [Matrix] falhou nos dois endpoints.")
                return None

    except Exception as e:
        print(f"❌ [Matrix] exceção no download HTTP: {e}")
        return None

async def upload_to_matrix(file_path, filename, content_type):
    """Upload de arquivo para o Matrix com tratamento de tuplas."""
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
                f,
                content_type=content_type,
                filename=filename,
                filesize=file_size
            )

        if isinstance(resp, tuple):
            resp = resp[0]

        if isinstance(resp, UploadResponse) and resp.content_uri:
            print(f"✅ [Matrix] upload: sucesso, URI: {resp.content_uri}")
            return resp.content_uri
        else:
            print(f"❌ [Matrix] upload: resposta inesperada: {resp}")
            return None

    except Exception as e:
        print(f"❌ [Matrix] upload: exceção: {e}")
        return None

async def upload_to_xmpp(file_path):
    if not xmpp_client:
        return None
    try:
        url = await xmpp_client['xep_0363'].upload_file(filename=str(file_path))
        print(f"✅ [XMPP] Upload com sucesso: {url}")
        return url
    except IqError as e:
        print(f"❌ [XMPP] IqError no Upload: {e.iq['error']['condition']} - {e.iq['error']['text']}")
    except Exception as e:
        print(f"❌ [XMPP] Erro genérico de Upload: {e}")
    return None

async def fetch_xmpp_media(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(url, timeout=5) as resp:
                ctype = resp.headers.get('Content-Type', '')
                if ctype.startswith(('image/', 'video/', 'audio/')):
                    ext = mimetypes.guess_extension(ctype.split(';')[0]) or ''
                    fname = url.split('/')[-1].split('?')[0]
                    fname = get_safe_filename(fname, ctype)
                    fpath = TEMP_DIR / f"xmpp_dl_{int(time.time())}_{fname}"
                    async with session.get(url, timeout=30) as get_resp:
                        if get_resp.status == 200:
                            with open(fpath, 'wb') as f:
                                f.write(await get_resp.read())
                            return fpath, fname, ctype
    except Exception as e:
        print(f"❌ [XMPP] Erro ao baixar mídia da URL {url}: {e}")
    return None, None, None

# ================== DISCORD WEBHOOKS ==================

async def get_or_create_webhook(channel_id):
    if channel_id in discord_webhooks:
        return discord_webhooks[channel_id]

    channel = discord_client.get_channel(channel_id)
    if not channel:
        print(f"❌ [Discord] Canal {channel_id} não encontrado")
        return None

    try:
        webhooks = await channel.webhooks()
        for wh in webhooks:
            if wh.name == DISCORD_WEBHOOK_NAME:
                discord_webhooks[channel_id] = wh
                print(f"✅ [Discord] Webhook existente encontrado: {wh.id}")
                return wh

        wh = await channel.create_webhook(name=DISCORD_WEBHOOK_NAME)
        discord_webhooks[channel_id] = wh
        print(f"✅ [Discord] Webhook criado: {wh.id}")
        return wh
    except Exception as e:
        print(f"❌ [Discord] Erro ao gerenciar webhook: {e}")
        return None

async def send_discord_webhook_message(channel_id, username, avatar_url, content=None, file=None, embeds=None):
    webhook = await get_or_create_webhook(channel_id)
    if not webhook:
        return None

    try:
        kwargs = {"content": content, "username": username, "wait": True}
        if avatar_url: kwargs["avatar_url"] = avatar_url
        if file: kwargs["file"] = file
        if embeds: kwargs["embeds"] = embeds

        msg = await webhook.send(**kwargs)
        print(f"✅ [Discord] Webhook enviado: {msg.id}")
        return msg
    except Exception as e:
        print(f"❌ [Discord] Erro webhook: {e}")
        return None

# ================== UTILITÁRIOS ==================

def get_safe_filename(original_name, content_type=None):
    """Garante que o arquivo temporário não contenha .bin e aplique a extensão certa."""
    safe = "".join(c for c in original_name if c.isalnum() or c in ' ._-').rstrip()
    if not safe:
        safe = "file"

    # Remove .bin do nome original enviado pelo cliente
    if safe.lower().endswith('.bin'):
        safe = safe[:-4]

    if content_type:
        ext = mimetypes.guess_extension(content_type.split(';')[0])
        if ext == '.jpe':
            ext = '.jpg'

        # Força extensão se a biblioteca falhar ou retornar .bin genérico
        if not ext or ext == '.bin':
            if 'image/jpeg' in content_type: ext = '.jpg'
            elif 'image/png' in content_type: ext = '.png'
            elif 'image/gif' in content_type: ext = '.gif'
            elif 'image/webp' in content_type: ext = '.webp'
            elif 'video/mp4' in content_type: ext = '.mp4'
            elif 'audio/ogg' in content_type: ext = '.ogg'
            else: ext = ''

        if ext and not safe.lower().endswith(ext.lower()):
            safe += ext

    return safe

def get_matrix_display_name(room, user_id):
    if room and hasattr(room, 'users'):
        user_info = room.users.get(user_id)
        if user_info and user_info.display_name:
            return user_info.display_name
    return user_id.split(':')[0].lstrip('@')

def escape_html(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def escape_discord_markdown(text):
    return text.replace('*', '\\*').replace('_', '\\_').replace('`', '\\`').replace('~', '\\~')

def markdown_to_html(text):
    return re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)

def get_media_type(content_type):
    if content_type.startswith('image/'): return 'm.image'
    elif content_type.startswith('video/'): return 'm.video'
    elif content_type.startswith('audio/'): return 'm.audio'
    else: return 'm.file'

def get_telegram_media_method(content_type):
    if content_type.startswith('image/'): return 'send_photo'
    elif content_type.startswith('video/') or content_type == 'application/x-matroska': return 'send_video'
    elif content_type.startswith('audio/'): return 'send_audio'
    else: return 'send_document'

def send_xmpp_media(client, room, sender, url, text_body=""):
    """Constrói e envia mensagem XMPP com suporte XHTML-IM e OOB para imagens nativas."""
    if not client or not client.is_connected():
        return
    try:
        msg = client.make_message(mto=room, mtype='groupchat')
        mbody = f"{sender} enviou mídia: {url}"
        if text_body:
            mbody += f"\n{text_body}"
        msg['body'] = mbody

        # XHTML-IM para exibição direta da imagem
        html_body = f"<p><b>{escape_html(sender)}</b> enviou mídia:<br/><a href='{url}'>{url}</a></p>"

        # Injeta a tag img se for um formato visual compatível
        if url.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
            html_body += f"<br/><img src='{url}' alt='Mídia recebida'/>"

        if text_body:
            html_body += f"<p>{escape_html(text_body)}</p>"

        msg['html']['body'] = html_body

        # OOB Data para clientes mais simples
        msg['oob']['url'] = url

        msg.send()
    except Exception as e:
        print(f"❌ [XMPP] Erro ao enviar mídia nativa: {e}")

# ================== EDIÇÕES & DELEÇÕES ==================

async def send_matrix_edit(room_id, event_id, new_content):
    if not matrix_client or not matrix_client.access_token: return
    content = {
        "msgtype": "m.text",
        "body": f" * {new_content['body']}",
        "m.new_content": new_content,
        "m.relates_to": {"rel_type": "m.replace", "event_id": event_id}
    }
    try:
        await matrix_client.room_send(room_id, "m.room.message", content)
    except Exception as e:
        print(f"❌ [Matrix] Erro ao editar: {e}")

async def send_discord_edit(channel_id, message_id, new_text):
    channel = discord_client.get_channel(channel_id)
    if channel:
        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(content=new_text)
        except Exception as e:
            print(f"❌ [Discord] Erro ao editar: {e}")

async def send_telegram_edit(chat_id, message_id, new_text):
    try:
        await telegram_bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=new_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"❌ [Telegram] Erro ao editar: {e}")

async def handle_matrix_redaction(room: MatrixRoom, event: RedactionEvent):
    if event.sender == matrix_client.user_id: return
    redacts = event.redacts
    if not redacts: return
    msg_map = load_message_map()
    if redacts not in msg_map: return
    target = msg_map[redacts]
    bridge = matrix_to_bridge.get(room.room_id)
    if not bridge: return

    if target['platform'] == 'discord':
        ch = discord_client.get_channel(target['channel_id'])
        if ch:
            try:
                msg = await ch.fetch_message(target['message_id'])
                await msg.delete()
            except Exception: pass
    elif target['platform'] == 'telegram':
        try:
            await telegram_bot.delete_message(chat_id=target['chat_id'], message_id=target['message_id'])
        except Exception: pass

@discord_client.event
async def on_message_delete(message):
    if message.author.id != discord_client.user.id: return
    msg_map = load_message_map()
    msg_id_str = str(message.id)
    if msg_id_str not in msg_map: return
    target = msg_map[msg_id_str]

    if target['platform'] == 'matrix' and matrix_client:
        try:
            await matrix_client.room_redact(target['room_id'], target['event_id'])
        except Exception: pass
    elif target['platform'] == 'telegram':
        try:
            await telegram_bot.delete_message(chat_id=target['chat_id'], message_id=target['message_id'])
        except Exception: pass

# ================== CALLBACK MATRIX ==================

async def matrix_message_callback(room: MatrixRoom, event: RoomMessage):
    if event.sender == matrix_client.user_id: return
    if room.room_id not in matrix_to_bridge: return
    bridge = matrix_to_bridge[room.room_id]

    ts = getattr(event, 'server_timestamp', 0)
    state = load_state()
    last_ts = state.get('last_ts', {}).get(room.room_id, 0)
    if ts <= last_ts: return
    state['last_ts'][room.room_id] = ts
    save_state(state)

    content = getattr(event, 'source', {}).get('content', {})
    relates_to = content.get('m.relates_to', {})

    if relates_to.get('rel_type') == 'm.replace':
        original = relates_to.get('event_id')
        new_body = content.get('m.new_content', {}).get('body', '')
        msg_map = load_message_map()
        if original in msg_map:
            target = msg_map[original]
            sender = get_matrix_display_name(room, event.sender)
            if target['platform'] == 'discord':
                await send_discord_edit(target['channel_id'], target['message_id'], f"**{sender}:** {new_body}")
            elif target['platform'] == 'telegram':
                await send_telegram_edit(target['chat_id'], target['message_id'], f"<b>{escape_html(sender)}:</b> {escape_html(new_body)}")
        return

    if not isinstance(event, (RoomMessageText, RoomMessageEmote, RoomMessageNotice, RoomMessageImage, RoomMessageVideo, RoomMessageAudio, RoomMessageMedia, RoomMessageFile)):
        return

    sender_display = get_matrix_display_name(room, event.sender)
    reply_to_event_id = relates_to.get('m.in_reply_to', {}).get('event_id')

    file_path = None
    filename = None
    content_type = None
    body_text = getattr(event, 'body', '')

    if isinstance(event, (RoomMessageImage, RoomMessageVideo, RoomMessageAudio, RoomMessageMedia, RoomMessageFile)):
        url = getattr(event, 'url', None)
        if url:
            filename_raw = getattr(event, 'body', 'media') or f"media_{event.event_id}"
            content_type = getattr(event, 'mimetype', 'application/octet-stream')

            safe_fname = get_safe_filename(filename_raw, content_type)
            file_path = TEMP_DIR / f"matrix_{event.event_id}_{safe_fname}"

            print(f"📥 [Matrix] Recebida mídia, baixando...")

            final_download_path = await download_matrix_file(matrix_client, url, str(file_path))
            if final_download_path:
                file_path = Path(final_download_path)
                filename = file_path.name
                text_to_send = f"**{sender_display}:** {body_text}" if body_text and body_text != filename_raw else f"**{sender_display}** enviou um arquivo"
            else:
                text_to_send = f"**{sender_display}** enviou mídia (falha download)"
                file_path = None
        else:
            text_to_send = f"**{sender_display}** enviou mídia sem URL"
    else:
        text_to_send = f"**{sender_display}:** {body_text}"
        if isinstance(event, RoomMessageEmote): text_to_send = f"* {sender_display} {body_text}"

    msg_map = load_message_map()
    reply_target_telegram = msg_map.get(reply_to_event_id, {}).get('message_id') if reply_to_event_id and msg_map.get(reply_to_event_id, {}).get('platform') == 'telegram' else None

    # Envia para Discord
    for ch_id in bridge.get('discord_channels', []):
        try:
            if file_path and file_path.exists():
                with open(file_path, 'rb') as f:
                    sent = await send_discord_webhook_message(ch_id, username=sender_display, avatar_url=None, content=text_to_send, file=DiscordFile(f, filename=filename))
            else:
                sent = await send_discord_webhook_message(ch_id, username=sender_display, avatar_url=None, content=text_to_send)

            if sent:
                msg_map[event.event_id] = {'platform': 'discord', 'channel_id': ch_id, 'message_id': sent.id, 'ts': time.time()}
                msg_map[str(sent.id)] = {'platform': 'matrix', 'room_id': room.room_id, 'event_id': event.event_id, 'ts': time.time()}
        except Exception as e: print(f"❌ [Matrix->Discord] Erro: {e}")

    # Envia para Telegram
    for tg_id in bridge.get('telegram_chats', []):
        try:
            kwargs = {}
            if reply_target_telegram: kwargs['reply_to_message_id'] = reply_target_telegram
            if file_path and file_path.exists():
                with open(file_path, 'rb') as f:
                    method_name = get_telegram_media_method(content_type)
                    if method_name == 'send_photo': sent = await telegram_bot.send_photo(chat_id=tg_id, photo=f, caption=markdown_to_html(text_to_send), parse_mode=ParseMode.HTML, **kwargs)
                    elif method_name == 'send_video': sent = await telegram_bot.send_video(chat_id=tg_id, video=f, caption=markdown_to_html(text_to_send), parse_mode=ParseMode.HTML, **kwargs)
                    elif method_name == 'send_audio': sent = await telegram_bot.send_audio(chat_id=tg_id, audio=f, caption=markdown_to_html(text_to_send), parse_mode=ParseMode.HTML, **kwargs)
                    else: sent = await telegram_bot.send_document(chat_id=tg_id, document=f, caption=markdown_to_html(text_to_send), filename=filename, parse_mode=ParseMode.HTML, **kwargs)
            else:
                sent = await telegram_bot.send_message(chat_id=tg_id, text=markdown_to_html(text_to_send), parse_mode=ParseMode.HTML, **kwargs)
            msg_map[event.event_id] = {'platform': 'telegram', 'chat_id': tg_id, 'message_id': sent.message_id, 'ts': time.time()}
            msg_map[str(sent.message_id)] = {'platform': 'matrix', 'room_id': room.room_id, 'event_id': event.event_id, 'ts': time.time()}
        except Exception as e: print(f"❌ [Matrix->Telegram] Erro: {e}")

    # Envia para XMPP
    for xmpp_room in bridge.get("xmpp_rooms", []):
        try:
            if file_path and file_path.exists():
                xmpp_url = await upload_to_xmpp(file_path)
                if xmpp_url:
                    send_xmpp_media(xmpp_client, xmpp_room, sender_display, xmpp_url, body_text)
            else:
                xmpp_client.send_message(mto=xmpp_room, mbody=text_to_send, mtype="groupchat")
        except Exception as e:
            print(f"❌ [Matrix->XMPP] Erro: {e}")

    if file_path and file_path.exists():
        file_path.unlink()

    save_message_map(msg_map)

# ================== CALLBACK DISCORD ==================

@discord_client.event
async def on_ready():
    print(f"✅ [Discord] Conectado: {discord_client.user}")

@discord_client.event
async def on_message(message):
    if message.author.id == discord_client.user.id or message.webhook_id: return
    if message.channel.id not in discord_to_bridge: return
    bridge = discord_to_bridge[message.channel.id]
    author = message.author.display_name

    reply_to_msg_id = message.reference.resolved.id if (message.reference and message.reference.resolved and isinstance(message.reference.resolved, discord.Message)) else None

    msg_map = load_message_map()
    reply_target_matrix = msg_map.get(str(reply_to_msg_id), {}).get('event_id') if reply_to_msg_id and msg_map.get(str(reply_to_msg_id), {}).get('platform') == 'matrix' else None
    reply_target_telegram = msg_map.get(str(reply_to_msg_id), {}).get('message_id') if reply_to_msg_id and msg_map.get(str(reply_to_msg_id), {}).get('platform') == 'telegram' else None

    if message.attachments:
        for att in message.attachments:
            content_type = att.content_type or 'application/octet-stream'
            safe_fname = get_safe_filename(att.filename, content_type)
            fpath = TEMP_DIR / f"discord_{att.id}_{safe_fname}"

            if await download_file_http(att.url, fpath):
                msgtype = get_media_type(content_type)

                # Matrix
                if matrix_client and matrix_client.access_token:
                    mxc = await upload_to_matrix(fpath, att.filename, content_type)
                    if mxc:
                        content = {"msgtype": msgtype, "body": att.filename, "url": mxc, "info": {"mimetype": content_type, "size": fpath.stat().st_size}}
                        if message.content:
                            content['body'] = message.content
                            content['format'] = "org.matrix.custom.html"
                            content['formatted_body'] = f"<b>{author}:</b> {message.content}"
                        if reply_target_matrix: content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_target_matrix}}
                        try: await matrix_client.room_send(bridge['matrix_room'], "m.room.message", content)
                        except Exception as e: print(f"❌ [Discord -> Matrix] Erro: {e}")

                # Telegram
                for tg_id in bridge.get('telegram_chats', []):
                    try:
                        with open(fpath, 'rb') as f:
                            caption = f"<b>{escape_html(author)}</b>" + (f": {escape_html(message.content)}" if message.content else "")
                            method_name = get_telegram_media_method(content_type)
                            if method_name == 'send_photo': sent = await telegram_bot.send_photo(chat_id=tg_id, photo=f, caption=caption, parse_mode=ParseMode.HTML, reply_to_message_id=reply_target_telegram)
                            elif method_name == 'send_video': sent = await telegram_bot.send_video(chat_id=tg_id, video=f, caption=caption, parse_mode=ParseMode.HTML, reply_to_message_id=reply_target_telegram)
                            elif method_name == 'send_audio': sent = await telegram_bot.send_audio(chat_id=tg_id, audio=f, caption=caption, parse_mode=ParseMode.HTML, reply_to_message_id=reply_target_telegram)
                            else: sent = await telegram_bot.send_document(chat_id=tg_id, document=f, caption=caption, filename=att.filename, parse_mode=ParseMode.HTML, reply_to_message_id=reply_target_telegram)
                        msg_map[str(sent.message_id)] = {'platform': 'discord', 'channel_id': message.channel.id, 'message_id': message.id, 'ts': time.time()}
                        msg_map[str(message.id)] = {'platform': 'telegram', 'chat_id': tg_id, 'message_id': sent.message_id, 'ts': time.time()}
                    except Exception as e: print(f"❌ [Discord -> Telegram] Erro: {e}")

                # XMPP
                for xmpp_room in bridge.get("xmpp_rooms", []):
                    try:
                        xmpp_url = await upload_to_xmpp(fpath)
                        if xmpp_url:
                            send_xmpp_media(xmpp_client, xmpp_room, author, xmpp_url, message.content)
                    except Exception as e: print(f"❌ [Discord -> XMPP] Mídia erro: {e}")

                fpath.unlink()

    elif message.content:
        text = f"**{author}:** {message.content}"

        # Matrix
        if matrix_client and matrix_client.access_token:
            content = {"msgtype": "m.text", "body": text, "format": "org.matrix.custom.html", "formatted_body": markdown_to_html(text)}
            if reply_target_matrix: content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_target_matrix}}
            try: await matrix_client.room_send(bridge['matrix_room'], "m.room.message", content)
            except Exception as e: pass

        # Telegram
        for tg_id in bridge.get('telegram_chats', []):
            try:
                sent = await telegram_bot.send_message(chat_id=tg_id, text=f"<b>{escape_html(author)}:</b> {escape_html(message.content)}", parse_mode=ParseMode.HTML, reply_to_message_id=reply_target_telegram)
                msg_map[str(sent.message_id)] = {'platform': 'discord', 'channel_id': message.channel.id, 'message_id': message.id, 'ts': time.time()}
                msg_map[str(message.id)] = {'platform': 'telegram', 'chat_id': tg_id, 'message_id': sent.message_id, 'ts': time.time()}
            except Exception as e: pass

        # XMPP
        for xmpp_room in bridge.get("xmpp_rooms", []):
            try:
                if xmpp_client and xmpp_client.is_connected():
                    xmpp_client.send_message(mto=xmpp_room, mbody=f"{author}: {message.content}", mtype="groupchat")
            except Exception as e: pass

    save_message_map(msg_map)

@discord_client.event
async def on_message_edit(before, after):
    if after.author.id == discord_client.user.id or after.webhook_id or before.content == after.content or after.channel.id not in discord_to_bridge: return
    msg_map = load_message_map()
    mid = str(after.id)
    if mid not in msg_map: return
    target = msg_map[mid]

    if target['platform'] == 'matrix' and matrix_client:
        new_content = {"msgtype": "m.text", "body": f"**{after.author.display_name}:** {after.content}", "format": "org.matrix.custom.html", "formatted_body": f"<b>{after.author.display_name}:</b> {after.content}"}
        await send_matrix_edit(target['room_id'], target['event_id'], new_content)
    elif target['platform'] == 'telegram':
        await send_telegram_edit(target['chat_id'], target['message_id'], f"<b>{escape_html(after.author.display_name)}:</b> {escape_html(after.content)}")

# ================== CALLBACK TELEGRAM ==================

async def telegram_message_callback(update: Update, context):
    if not update.message: return
    chat_id = update.effective_chat.id
    if chat_id not in telegram_to_bridge: return
    bridge = telegram_to_bridge[chat_id]

    author = update.effective_user.full_name or update.effective_user.first_name
    reply_id = update.message.reply_to_message.message_id if update.message.reply_to_message else None

    msg_map = load_message_map()
    reply_target_matrix = msg_map.get(str(reply_id), {}).get('event_id') if reply_id and msg_map.get(str(reply_id), {}).get('platform') == 'matrix' else None

    file_path = None
    caption = update.message.caption or ""
    filename = None
    content_type = None
    msgtype = None

    if update.message.photo:
        p = update.message.photo[-1]
        file = await p.get_file()
        filename = f"photo_{update.message.message_id}.jpg"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = 'image/jpeg'
        msgtype = 'm.image'
    elif update.message.video:
        v = update.message.video
        file = await v.get_file()
        filename = v.file_name or f"video_{update.message.message_id}.mp4"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = v.mime_type or 'video/mp4'
        msgtype = 'm.video'
    elif update.message.voice:
        v = update.message.voice
        file = await v.get_file()
        filename = f"voice_{update.message.message_id}.ogg"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = v.mime_type or 'audio/ogg'
        msgtype = 'm.audio'
    elif update.message.document:
        d = update.message.document
        file = await d.get_file()
        filename = d.file_name or f"doc_{update.message.message_id}.bin"
        file_path = TEMP_DIR / filename
        await file.download_to_drive(file_path)
        content_type = d.mime_type or 'application/octet-stream'
        msgtype = 'm.file'

    if file_path and file_path.exists():
        # Matrix
        if matrix_client and matrix_client.access_token:
            mxc = await upload_to_matrix(file_path, filename, content_type)
            if mxc:
                content = {"msgtype": msgtype, "body": caption or filename, "url": mxc, "info": {"mimetype": content_type, "size": file_path.stat().st_size}}
                if caption:
                    content['body'] = caption
                    content['format'] = "org.matrix.custom.html"
                    content['formatted_body'] = markdown_to_html(caption)
                if reply_target_matrix: content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_target_matrix}}
                try: await matrix_client.room_send(bridge['matrix_room'], "m.room.message", content)
                except Exception as e: print(f"❌ [Telegram->Matrix] Erro: {e}")

        # Discord
        for ch_id in bridge.get('discord_channels', []):
            try:
                with open(file_path, 'rb') as f:
                    sent = await send_discord_webhook_message(ch_id, username=author, avatar_url=None, content=f"{caption}", file=DiscordFile(f, filename=filename))
                    if sent:
                        msg_map[str(sent.id)] = {'platform': 'telegram', 'chat_id': chat_id, 'message_id': update.message.message_id, 'ts': time.time()}
                        msg_map[str(update.message.message_id)] = {'platform': 'discord', 'channel_id': ch_id, 'message_id': sent.id, 'ts': time.time()}
            except Exception as e: print(f"❌ [Telegram->Discord] Erro: {e}")

        # XMPP
        for xmpp_room in bridge.get("xmpp_rooms", []):
            try:
                xmpp_url = await upload_to_xmpp(file_path)
                if xmpp_url:
                    send_xmpp_media(xmpp_client, xmpp_room, author, xmpp_url, caption)
            except Exception as e: print(f"❌ [Telegram -> XMPP] Erro mídia: {e}")

        file_path.unlink()

    elif update.message.text:
        text = f"**{author}:** {update.message.text}"

        # Matrix
        if matrix_client and matrix_client.access_token:
            content = {"msgtype": "m.text", "body": text, "format": "org.matrix.custom.html", "formatted_body": markdown_to_html(text)}
            if reply_target_matrix: content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_target_matrix}}
            try: await matrix_client.room_send(bridge['matrix_room'], "m.room.message", content)
            except Exception: pass

        # Discord
        for ch_id in bridge.get('discord_channels', []):
            try:
                sent = await send_discord_webhook_message(ch_id, username=author, avatar_url=None, content=update.message.text)
                if sent:
                    msg_map[str(sent.id)] = {'platform': 'telegram', 'chat_id': chat_id, 'message_id': update.message.message_id, 'ts': time.time()}
                    msg_map[str(update.message.message_id)] = {'platform': 'discord', 'channel_id': ch_id, 'message_id': sent.id, 'ts': time.time()}
            except Exception: pass

        # XMPP
        for xmpp_room in bridge.get("xmpp_rooms", []):
            try:
                if xmpp_client and xmpp_client.is_connected():
                    xmpp_client.send_message(mto=xmpp_room, mbody=f"{author}: {update.message.text}", mtype="groupchat")
            except Exception: pass

    save_message_map(msg_map)

async def telegram_edit_callback(update: Update, context):
    if not update.edited_message: return
    msg = update.edited_message
    if update.effective_chat.id not in telegram_to_bridge: return
    msg_map = load_message_map()
    mid = str(msg.message_id)
    if mid not in msg_map: return
    target = msg_map[mid]
    author = msg.from_user.full_name or msg.from_user.first_name

    if target['platform'] == 'matrix' and matrix_client:
        new_content = {"msgtype": "m.text", "body": f"**{author}:** {msg.text}", "format": "org.matrix.custom.html", "formatted_body": f"<b>{author}:</b> {msg.text}"}
        await send_matrix_edit(target['room_id'], target['event_id'], new_content)
    elif target['platform'] == 'discord':
        await send_discord_edit(target['channel_id'], target['message_id'], f"**{author}:** {msg.text}")

telegram_app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), telegram_message_callback))
telegram_app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, telegram_edit_callback))

# ================== XMPP CLIENT ==================

class XMPPClient(slixmpp.ClientXMPP):
    def __init__(self, jid, password, bridges):
        super().__init__(jid, password)
        self.bridges = bridges
        self.register_plugin('xep_0030')
        self.register_plugin('xep_0045')
        self.register_plugin('xep_0066') # Out of Band Data para Mídia
        self.register_plugin('xep_0071') # XHTML-IM para exibição de imagens
        self.register_plugin('xep_0199')
        self.register_plugin("xep_0004")
        self.register_plugin("xep_0249")
        self.register_plugin("xep_0363")

        self.add_event_handler("session_start", self.start)
        self.add_event_handler("groupchat_message", self.handle_group_message)

    async def start(self, event):
        self.send_presence()
        await self.get_roster()

        for bridge in self.bridges:
            for room in bridge.get('xmpp_rooms', []):
                try:
                    await asyncio.to_thread(self.plugin['xep_0045'].join_muc, room, XMPP_JID.split('@')[0])
                    print(f"✅ [XMPP] Entrou em {room}")
                except Exception as e:
                    print(f"❌ [XMPP] Erro ao entrar em {room}: {e}")

    async def handle_group_message(self, msg):
        if msg["type"] != "groupchat": return
        room = msg["from"].bare
        sender = msg["mucnick"]

        if sender == XMPP_JID.split("@")[0]: return
        body = msg["body"]
        if not body: return

        bridge = next((b for b in self.bridges if room in b.get("xmpp_rooms", [])), None)
        if not bridge: return

        urls = re.findall(r'(https?://[^\s]+)', body)
        media_files = []
        if urls:
            results = await asyncio.gather(*(fetch_xmpp_media(u) for u in urls))
            media_files = [r for r in results if r[0] is not None]

        body_stripped = body
        for u in urls: body_stripped = body_stripped.replace(u, '').strip()
        is_only_urls = (body_stripped == "" and len(media_files) > 0)

        # ---------- Matrix ----------
        if matrix_client and matrix_client.access_token and bridge.get("matrix_room"):
            try:
                if media_files:
                    for fpath, fname, ctype in media_files:
                        mxc = await upload_to_matrix(fpath, fname, ctype)
                        if mxc:
                            content = {"msgtype": get_media_type(ctype), "body": fname, "url": mxc, "info": {"mimetype": ctype, "size": fpath.stat().st_size}}
                            await matrix_client.room_send(bridge["matrix_room"], "m.room.message", content)

                if not is_only_urls:
                    text = f"**{sender}:** {body}"
                    content = {"msgtype": "m.text", "body": text, "format": "org.matrix.custom.html", "formatted_body": markdown_to_html(text)}
                    await matrix_client.room_send(bridge["matrix_room"], "m.room.message", content)
            except Exception as e: print(f"❌ [XMPP -> Matrix] {e}")

        # ---------- Discord ----------
        for ch_id in bridge.get("discord_channels", []):
            try:
                if media_files:
                    first_file = True
                    for fpath, fname, ctype in media_files:
                        with open(fpath, 'rb') as f:
                            await send_discord_webhook_message(
                                channel_id=ch_id, username=sender, avatar_url=None,
                                content=body if first_file and not is_only_urls else "",
                                file=DiscordFile(f, filename=fname)
                            )
                        first_file = False
                elif not is_only_urls:
                    await send_discord_webhook_message(channel_id=ch_id, username=sender, avatar_url=None, content=body)
            except Exception as e: print(f"❌ [XMPP -> Discord] {e}")

        # ---------- Telegram ----------
        for tg_id in bridge.get("telegram_chats", []):
            try:
                if media_files:
                    first_file = True
                    for fpath, fname, ctype in media_files:
                        with open(fpath, 'rb') as f:
                            m = get_telegram_media_method(ctype)
                            caption = f"<b>{escape_html(sender)}</b>: {escape_html(body)}" if first_file and not is_only_urls else ""
                            if m == 'send_photo': await telegram_bot.send_photo(chat_id=tg_id, photo=f, caption=caption, parse_mode=ParseMode.HTML)
                            elif m == 'send_video': await telegram_bot.send_video(chat_id=tg_id, video=f, caption=caption, parse_mode=ParseMode.HTML)
                            elif m == 'send_audio': await telegram_bot.send_audio(chat_id=tg_id, audio=f, caption=caption, parse_mode=ParseMode.HTML)
                            else: await telegram_bot.send_document(chat_id=tg_id, document=f, caption=caption, parse_mode=ParseMode.HTML)
                        first_file = False
                elif not is_only_urls:
                    await telegram_bot.send_message(chat_id=tg_id, text=f"<b>{escape_html(sender)}</b>: {escape_html(body)}", parse_mode=ParseMode.HTML)
            except Exception as e: print(f"❌ [XMPP -> Telegram] {e}")

        # Cleanup
        for fpath, _, _ in media_files:
            if fpath.exists(): fpath.unlink()

async def run_xmpp_client():
    global xmpp_client
    xmpp_client = XMPPClient(XMPP_JID, XMPP_PASSWORD, BRIDGES)

    if not await xmpp_client.connect((XMPP_SERVER, XMPP_PORT)):
        print("❌ Falha ao conectar ao XMPP")
        return

    print(f"✅ [XMPP] Conectado: {XMPP_JID}")
    while xmpp_client.is_connected():
        await asyncio.sleep(1)
    print("❌ XMPP desconectado.")

# ================== LOOP DE RECONEXÃO MATRIX ==================

async def run_matrix_sync():
    global matrix_client
    while True:
        try:
            matrix_client = await matrix_login()
            if not matrix_client:
                await asyncio.sleep(60)
                continue

            for ec in [RoomMessageText, RoomMessageEmote, RoomMessageNotice, RoomMessageMedia, RoomMessageFile, RoomMessageImage, RoomMessageVideo, RoomMessageAudio]:
                matrix_client.add_event_callback(matrix_message_callback, ec)
            matrix_client.add_event_callback(handle_matrix_redaction, RedactionEvent)

            for bridge in BRIDGES:
                room_id = bridge.get('matrix_room')
                if not room_id: continue
                try: await matrix_client.join(room_id)
                except Exception as e: print(f"❌ [Matrix] Erro ao entrar na sala {room_id}: {e}")

            state = load_state()
            sync_token = state.get('sync_token')
            await matrix_client.sync_forever(timeout=30000, since=sync_token)

        except asyncio.CancelledError:
            break
        except Exception as e:
            if matrix_client:
                await matrix_client.close()
                matrix_client = None
            await asyncio.sleep(60)

# ================== MAIN ==================

async def main():
    discord_task = asyncio.create_task(discord_client.start(DISCORD_TOKEN))

    await telegram_app.initialize()
    await telegram_app.updater.start_polling()
    await telegram_app.start()

    matrix_task = asyncio.create_task(run_matrix_sync())
    xmpp_task = asyncio.create_task(run_xmpp_client())

    try:
        await asyncio.gather(discord_task, matrix_task, xmpp_task)
    except KeyboardInterrupt:
        pass
    finally:
        if matrix_client and matrix_client.next_batch:
            state = load_state()
            state['sync_token'] = matrix_client.next_batch
            save_state(state)
        if matrix_client: await matrix_client.close()
        if xmpp_client and xmpp_client.is_connected(): xmpp_client.disconnect()
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
