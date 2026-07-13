import asyncio
import json
import os
import tempfile
import mimetypes
import re
import time
import aiohttp
import xml.etree.ElementTree as ET
from pathlib import Path
from nio import (
    AsyncClient, RoomMessageText, RoomMessageEmote, RoomMessageNotice,
    RoomMessageMedia, RoomMessageFile, RoomMessageImage, RoomMessageVideo,
    RoomMessageAudio, RoomMessage, MatrixRoom, UploadResponse, RedactionEvent,
    RoomSendResponse
)
import discord
from discord import File as DiscordFile
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, MessageHandler, filters
from telegram.constants import ParseMode
import slixmpp
from slixmpp.exceptions import IqError, IqTimeout

# ================== CONFIGURAÇÕES ==================
with open('config.json', 'r') as f:
    config = json.load(f)

MATRIX_HOMESERVER = config['matrix']['homeserver']
MATRIX_USERNAME = config['matrix']['username']
MATRIX_PASSWORD = config['matrix']['password']

DISCORD_TOKEN = config['discord']['token']
DISCORD_WEBHOOK_NAME = config['discord'].get('webhook_name', '🌉 Bridge Bot')

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

# ================== CLIENTES GLOBAIS ==================
matrix_client = None
discord_client = discord.Client(intents=discord.Intents.all())
telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
telegram_bot = telegram_app.bot
xmpp_client = None

discord_webhooks = {}

matrix_to_bridge = {}
discord_to_bridge = {}
telegram_to_bridge = {}
xmpp_to_bridge = {}

for bridge in BRIDGES:
    room_id = bridge.get('matrix_room')
    if room_id: matrix_to_bridge[room_id] = bridge
    for ch in bridge.get('discord_channels', []): discord_to_bridge[ch] = bridge
    for tg in bridge.get('telegram_chats', []): telegram_to_bridge[tg] = bridge
    for xmpp_room in bridge.get('xmpp_rooms', []): xmpp_to_bridge[xmpp_room] = bridge

# ================== PERSISTÊNCIA UNIFICADA ==================

def load_state():
    try:
        with open(STATE_FILE, 'r') as f: return json.load(f)
    except: return {"sync_token": None, "last_ts": {}}

def save_state(state):
    with open(STATE_FILE, 'w') as f: json.dump(state, f)

def load_message_map():
    try:
        with open(MESSAGE_MAP_FILE, 'r') as f: return json.load(f)
    except: return {}

def save_message_map(map_data):
    now = time.time()
    to_delete = [k for k, v in map_data.items() if isinstance(v, dict) and 'ts' in v and now - v['ts'] > 2592000]
    for k in to_delete:
        for val in map_data[k].values():
            if isinstance(val, str) and val in map_data:
                del map_data[val]
        del map_data[k]
    with open(MESSAGE_MAP_FILE, 'w') as f: json.dump(map_data, f)

def register_message(source_platform, source_id, sent_mappings, author="Bot", text=""):
    msg_map = load_message_map()
    uni_id = f"{source_platform}_{source_id}"

    preview = str(text)[:60].replace('\n', ' ') + ('...' if len(str(text)) > 60 else '') if text else 'Mídia/Arquivo'

    if uni_id not in msg_map:
        msg_map[uni_id] = {
            'ts': time.time(),
            source_platform: str(source_id),
            'author': author,
            'preview': preview
        }

    for plat, p_id in sent_mappings.items():
        if p_id:
            msg_map[uni_id][plat] = str(p_id)
            msg_map[str(p_id)] = uni_id

    save_message_map(msg_map)

def get_reply_targets(reply_to_id):
    if not reply_to_id: return {}
    msg_map = load_message_map()
    reply_to_id = str(reply_to_id)

    if reply_to_id in msg_map:
        val = msg_map[reply_to_id]
        if isinstance(val, str): return msg_map.get(val, {})
        elif isinstance(val, dict): return val
    return {}

def get_fallback_quote(reply_targets):
    if not reply_targets: return ""
    if 'author' in reply_targets or 'preview' in reply_targets:
        author = reply_targets.get('author', 'Alguém')
        preview = reply_targets.get('preview', 'Mídia')
        return f"> **{author}**: {preview}\n"
    return "> [Em resposta a uma mensagem antiga]\n"

# ================== FUNÇÕES DE REDE E UPLOADS ==================

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

async def download_matrix_file(client, url, output_path):
    if not url.startswith('mxc://'): return None
    parts = url[6:].split('/')
    server_name, media_id = parts[0], parts[1]
    base_url = client.homeserver.rstrip('/')
    headers = {"Authorization": f"Bearer {client.access_token}"}

    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.get(f"{base_url}/_matrix/client/v1/media/download/{server_name}/{media_id}", headers=headers)
            if resp.status != 200:
                resp = await session.get(f"{base_url}/_matrix/media/v3/download/{server_name}/{media_id}", headers=headers)

            if resp.status == 200:
                content_type = resp.headers.get('Content-Type', '')
                ext = mimetypes.guess_extension(content_type)
                if ext == '.jpe': ext = '.jpg'
                if not ext or ext == '.bin':
                    if 'image/jpeg' in content_type: ext = '.jpg'
                    elif 'image/png' in content_type: ext = '.png'
                    elif 'video/mp4' in content_type: ext = '.mp4'
                    elif 'audio/ogg' in content_type: ext = '.ogg'
                    else: ext = ''
                final_path = f"{output_path}{ext}" if ext and not output_path.endswith(ext) else output_path
                with open(final_path, 'wb') as f: f.write(await resp.read())
                return final_path
    except Exception as e: print(f"❌ [Matrix] exceção HTTP: {e}")
    return None

async def upload_to_matrix(file_path, filename, content_type):
    if not matrix_client or not matrix_client.access_token: return None
    file_path = Path(file_path)
    if not file_path.exists() or file_path.stat().st_size == 0: return None
    try:
        with open(file_path, 'rb') as f:
            resp = await matrix_client.upload(f, content_type=content_type, filename=filename, filesize=file_path.stat().st_size)
        if isinstance(resp, tuple): resp = resp[0]
        if isinstance(resp, UploadResponse) and resp.content_uri: return resp.content_uri
    except Exception as e: print(f"❌ [Matrix] upload exceção: {e}")
    return None

async def upload_to_xmpp(file_path):
    if not xmpp_client: return None
    try:
        url = await xmpp_client['xep_0363'].upload_file(filename=str(file_path))
        return url
    except Exception as e: print(f"❌ [XMPP] Erro Genérico de Upload: {e}")
    return None

async def fetch_xmpp_media(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(url, timeout=5) as resp:
                ctype = resp.headers.get('Content-Type', '')
                if ctype.startswith(('image/', 'video/', 'audio/')):
                    fname = get_safe_filename(url.split('/')[-1].split('?')[0], ctype)
                    fpath = TEMP_DIR / f"xmpp_dl_{int(time.time())}_{fname}"
                    async with session.get(url, timeout=30) as get_resp:
                        if get_resp.status == 200:
                            with open(fpath, 'wb') as f: f.write(await get_resp.read())
                            return fpath, fname, ctype
    except Exception: pass
    return None, None, None

# ================== XMPP & DISCORD HELPERS ==================

def send_xmpp_text(client, room, text, html_body=None, reply_to_id=None):
    if not client or not client.is_connected(): return None
    try:
        msg = client.make_message(mto=room, mtype='groupchat')
        msg_id = client.new_id()
        msg['id'] = msg_id
        msg['body'] = text
        if html_body: msg['html']['body'] = html_body

        if reply_to_id:
            reply_elem = ET.Element('{urn:xmpp:reply:0}reply', {'id': str(reply_to_id)})
            msg.xml.append(reply_elem)

        msg.send()
        return msg_id
    except Exception as e:
        print(f"❌ [XMPP] Erro texto: {e}")
        return None

def send_xmpp_media(client, room, sender, url, text_body="", reply_to_id=None):
    if not client or not client.is_connected(): return None
    try:
        msg = client.make_message(mto=room, mtype='groupchat')
        msg_id = client.new_id()
        msg['id'] = msg_id
        mbody = f"{sender} enviou mídia: {url}\n{text_body}" if text_body else f"{sender} enviou mídia: {url}"
        msg['body'] = mbody
        html_body = f"<p><b>{escape_html(sender)}</b> enviou mídia:<br/><a href='{url}'>{url}</a></p>"
        if url.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
            html_body += f"<br/><img src='{url}' alt='Mídia recebida'/>"
        if text_body: html_body += f"<p>{escape_html(text_body)}</p>"
        msg['html']['body'] = html_body
        msg['oob']['url'] = url

        if reply_to_id:
            reply_elem = ET.Element('{urn:xmpp:reply:0}reply', {'id': str(reply_to_id)})
            msg.xml.append(reply_elem)

        msg.send()
        return msg_id
    except Exception as e:
        print(f"❌ [XMPP] Erro mídia: {e}")
        return None

def send_xmpp_edit(client, room, text, replace_id):
    if not client or not client.is_connected() or not replace_id: return
    try:
        msg = client.make_message(mto=room, mtype='groupchat')
        msg['body'] = text
        replace = ET.Element('{urn:xmpp:message-correct:0}replace', {'id': str(replace_id)})
        msg.xml.append(replace)
        msg.send()
    except Exception: pass

async def get_or_create_webhook(channel_id):
    if channel_id in discord_webhooks: return discord_webhooks[channel_id]
    channel = discord_client.get_channel(channel_id)
    if not channel: return None
    try:
        for wh in await channel.webhooks():
            if wh.name == DISCORD_WEBHOOK_NAME:
                discord_webhooks[channel_id] = wh
                return wh
        wh = await channel.create_webhook(name=DISCORD_WEBHOOK_NAME)
        discord_webhooks[channel_id] = wh
        return wh
    except Exception: return None

async def send_discord_webhook_message(channel_id, username, avatar_url, content=None, file=None, embeds=None):
    webhook = await get_or_create_webhook(channel_id)
    if not webhook: return None
    try:
        kwargs = {"content": content, "username": username, "wait": True}
        if avatar_url: kwargs["avatar_url"] = avatar_url
        if file: kwargs["file"] = file
        if embeds: kwargs["embeds"] = embeds
        return await webhook.send(**kwargs)
    except Exception: return None

async def send_discord_edit(channel_id, message_id, new_text):
    if not message_id: return
    webhook = await get_or_create_webhook(channel_id)
    if webhook:
        try: await webhook.edit_message(message_id, content=new_text)
        except Exception: pass

async def send_telegram_edit(chat_id, message_id, new_text):
    if not message_id: return
    try: await telegram_bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=new_text, parse_mode=ParseMode.HTML)
    except Exception: pass

async def send_matrix_edit(room_id, event_id, new_content):
    if not matrix_client or not matrix_client.access_token or not event_id: return
    content = {
        "msgtype": "m.text", "body": f" * {new_content['body']}",
        "m.new_content": new_content,
        "m.relates_to": {"rel_type": "m.replace", "event_id": event_id}
    }
    try: await matrix_client.room_send(room_id, "m.room.message", content)
    except Exception: pass

# ================== UTILITÁRIOS ==================

def get_safe_filename(original_name, content_type=None):
    safe = "".join(c for c in original_name if c.isalnum() or c in ' ._-').rstrip()
    if not safe: safe = "file"
    if safe.lower().endswith('.bin'): safe = safe[:-4]
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(';')[0])
        if ext == '.jpe': ext = '.jpg'
        if not ext or ext == '.bin':
            if 'image/jpeg' in content_type: ext = '.jpg'
            elif 'image/png' in content_type: ext = '.png'
            elif 'video/mp4' in content_type: ext = '.mp4'
            else: ext = ''
        if ext and not safe.lower().endswith(ext.lower()): safe += ext
    return safe

def get_matrix_display_name(room, user_id):
    if room and hasattr(room, 'users'):
        user_info = room.users.get(user_id)
        if user_info and user_info.display_name: return user_info.display_name
    return user_id.split(':')[0].lstrip('@')

def escape_html(text): return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
def markdown_to_html(text): return re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
def get_media_type(content_type):
    if content_type.startswith('image/'): return 'm.image'
    elif content_type.startswith('video/'): return 'm.video'
    elif content_type.startswith('audio/'): return 'm.audio'
    return 'm.file'
def get_telegram_media_method(content_type):
    if content_type.startswith('image/'): return 'send_photo'
    elif content_type.startswith('video/') or content_type == 'application/x-matroska': return 'send_video'
    elif content_type.startswith('audio/'): return 'send_audio'
    return 'send_document'

# ================== CALLBACKS ==================

async def handle_matrix_redaction(room: MatrixRoom, event: RedactionEvent):
    if event.sender == matrix_client.user_id: return
    if not event.redacts: return
    targets = get_reply_targets(event.redacts)
    bridge = matrix_to_bridge.get(room.room_id)
    if not bridge: return

    for ch_id in bridge.get('discord_channels', []):
        webhook = await get_or_create_webhook(ch_id)
        if webhook and targets.get('discord'):
            try: await webhook.delete_message(targets.get('discord'))
            except Exception: pass
    for tg_id in bridge.get('telegram_chats', []):
        if targets.get('telegram'):
            try: await telegram_bot.delete_message(chat_id=tg_id, message_id=targets.get('telegram'))
            except Exception: pass

async def matrix_message_callback(room: MatrixRoom, event: RoomMessage):
    if event.sender == matrix_client.user_id: return
    bridge = matrix_to_bridge.get(room.room_id)
    if not bridge: return

    ts = getattr(event, 'server_timestamp', 0)
    state = load_state()
    if ts <= state.get('last_ts', {}).get(room.room_id, 0): return
    state.setdefault('last_ts', {})[room.room_id] = ts
    save_state(state)

    content = getattr(event, 'source', {}).get('content', {})
    relates_to = content.get('m.relates_to', {})

    if relates_to.get('rel_type') == 'm.replace':
        original = relates_to.get('event_id')
        new_body = content.get('m.new_content', {}).get('body', '')
        targets = get_reply_targets(original)
        sender = get_matrix_display_name(room, event.sender)

        for ch_id in bridge.get('discord_channels', []):
            await send_discord_edit(ch_id, targets.get('discord'), f"**{sender}:** {new_body}")
        for tg_id in bridge.get('telegram_chats', []):
            await send_telegram_edit(tg_id, targets.get('telegram'), f"<b>{escape_html(sender)}:</b> {escape_html(new_body)}")
        for xmpp_room in bridge.get('xmpp_rooms', []):
            send_xmpp_edit(xmpp_client, xmpp_room, f"{sender}: {new_body}", targets.get('xmpp'))
        return

    if not isinstance(event, (RoomMessageText, RoomMessageEmote, RoomMessageNotice, RoomMessageImage, RoomMessageVideo, RoomMessageAudio, RoomMessageMedia, RoomMessageFile)): return

    sender_display = get_matrix_display_name(room, event.sender)
    reply_to_event_id = relates_to.get('m.in_reply_to', {}).get('event_id')
    reply_targets = get_reply_targets(reply_to_event_id)
    fallback_quote = get_fallback_quote(reply_targets)

    file_path, filename, content_type = None, None, None
    body_text = getattr(event, 'body', '')

    if isinstance(event, (RoomMessageImage, RoomMessageVideo, RoomMessageAudio, RoomMessageMedia, RoomMessageFile)):
        url = getattr(event, 'url', None)
        if url:
            filename_raw = getattr(event, 'body', 'media')
            content_type = getattr(event, 'mimetype', 'application/octet-stream')
            safe_fname = get_safe_filename(filename_raw, content_type)
            file_path = TEMP_DIR / f"matrix_{event.event_id}_{safe_fname}"
            final_path = await download_matrix_file(matrix_client, url, str(file_path))
            if final_path:
                file_path = Path(final_path)
                filename = file_path.name
                text_to_send = f"**{sender_display}:** {body_text}" if body_text and body_text != filename_raw else f"**{sender_display}** enviou um arquivo"
            else:
                text_to_send = f"**{sender_display}** enviou mídia (falha download)"; file_path = None
    else:
        text_to_send = f"**{sender_display}:** {body_text}"

    discord_text = f"{fallback_quote}{text_to_send}"
    xmpp_text = f"{fallback_quote}{text_to_send}"
    sent_mappings = {}

    for ch_id in bridge.get('discord_channels', []):
        try:
            if file_path and file_path.exists():
                with open(file_path, 'rb') as f:
                    sent = await send_discord_webhook_message(ch_id, sender_display, None, discord_text, DiscordFile(f, filename=filename))
            else:
                sent = await send_discord_webhook_message(ch_id, sender_display, None, discord_text)
            if sent: sent_mappings['discord'] = sent.id
        except Exception: pass

    for tg_id in bridge.get('telegram_chats', []):
        try:
            kwargs = {}
            if reply_targets.get('telegram'): kwargs['reply_to_message_id'] = reply_targets['telegram']
            if file_path and file_path.exists():
                with open(file_path, 'rb') as f:
                    method = get_telegram_media_method(content_type)
                    if method == 'send_photo': sent = await telegram_bot.send_photo(chat_id=tg_id, photo=f, caption=markdown_to_html(text_to_send), parse_mode=ParseMode.HTML, **kwargs)
                    elif method == 'send_video': sent = await telegram_bot.send_video(chat_id=tg_id, video=f, caption=markdown_to_html(text_to_send), parse_mode=ParseMode.HTML, **kwargs)
                    elif method == 'send_audio': sent = await telegram_bot.send_audio(chat_id=tg_id, audio=f, caption=markdown_to_html(text_to_send), parse_mode=ParseMode.HTML, **kwargs)
                    else: sent = await telegram_bot.send_document(chat_id=tg_id, document=f, caption=markdown_to_html(text_to_send), parse_mode=ParseMode.HTML, **kwargs)
            else:
                sent = await telegram_bot.send_message(chat_id=tg_id, text=markdown_to_html(text_to_send), parse_mode=ParseMode.HTML, **kwargs)
            if sent: sent_mappings['telegram'] = sent.message_id
        except Exception: pass

    for xmpp_room in bridge.get('xmpp_rooms', []):
        if file_path and file_path.exists():
            url = await upload_to_xmpp(file_path)
            if url:
                m_id = send_xmpp_media(xmpp_client, xmpp_room, sender_display, url, body_text if body_text != filename else "", reply_targets.get('xmpp'))
                if m_id: sent_mappings['xmpp'] = m_id
            else:
                m_id = send_xmpp_text(xmpp_client, xmpp_room, f"{sender_display} enviou mídia (falha upload)", None, reply_targets.get('xmpp'))
                if m_id: sent_mappings['xmpp'] = m_id
        else:
            m_id = send_xmpp_text(xmpp_client, xmpp_room, f"{sender_display}: {body_text}", None, reply_targets.get('xmpp'))
            if m_id: sent_mappings['xmpp'] = m_id

    register_message('matrix', event.event_id, sent_mappings, sender_display, body_text)
    if file_path and file_path.exists(): file_path.unlink()

@discord_client.event
async def on_message(message):
    if message.author.bot or message.author == discord_client.user: return
    bridge = discord_to_bridge.get(message.channel.id)
    if not bridge: return

    author = message.author.display_name
    text = message.content
    reply_targets = get_reply_targets(message.reference.message_id) if message.reference else {}
    fallback_quote = get_fallback_quote(reply_targets)

    file_path, filename, content_type = None, None, None
    if message.attachments:
        att = message.attachments[0]
        filename = get_safe_filename(att.filename, att.content_type)
        file_path = TEMP_DIR / f"discord_{message.id}_{filename}"
        await att.save(file_path)
        content_type = att.content_type or 'application/octet-stream'

    sent_mappings = {}

    for room_id in [bridge.get('matrix_room')] if bridge.get('matrix_room') else []:
        if not matrix_client: continue
        content = {"msgtype": "m.text", "body": f"{author}: {text}"} if text else {"msgtype": "m.text", "body": f"{author} enviou mídia"}
        if reply_targets.get('matrix'): content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_targets['matrix']}}

        if file_path and file_path.exists():
            uri = await upload_to_matrix(file_path, filename, content_type)
            if uri:
                content['msgtype'] = get_media_type(content_type)
                content['url'] = uri
                content['body'] = filename

        try:
            resp = await matrix_client.room_send(room_id, "m.room.message", content)
            if isinstance(resp, RoomSendResponse): sent_mappings['matrix'] = resp.event_id
        except Exception: pass

    for tg_id in bridge.get('telegram_chats', []):
        try:
            tg_text = f"<b>{escape_html(author)}:</b> {escape_html(text)}" if text else f"<b>{escape_html(author)}</b> enviou mídia"
            kwargs = {}
            if reply_targets.get('telegram'): kwargs['reply_to_message_id'] = reply_targets['telegram']
            if file_path and file_path.exists():
                with open(file_path, 'rb') as f:
                    method = get_telegram_media_method(content_type)
                    if method == 'send_photo': sent = await telegram_bot.send_photo(chat_id=tg_id, photo=f, caption=tg_text, parse_mode=ParseMode.HTML, **kwargs)
                    elif method == 'send_video': sent = await telegram_bot.send_video(chat_id=tg_id, video=f, caption=tg_text, parse_mode=ParseMode.HTML, **kwargs)
                    elif method == 'send_audio': sent = await telegram_bot.send_audio(chat_id=tg_id, audio=f, caption=tg_text, parse_mode=ParseMode.HTML, **kwargs)
                    else: sent = await telegram_bot.send_document(chat_id=tg_id, document=f, caption=tg_text, parse_mode=ParseMode.HTML, **kwargs)
            else:
                sent = await telegram_bot.send_message(chat_id=tg_id, text=tg_text, parse_mode=ParseMode.HTML, **kwargs)
            if sent: sent_mappings['telegram'] = sent.message_id
        except Exception: pass

    for xmpp_room in bridge.get('xmpp_rooms', []):
        xmpp_text = f"{fallback_quote}{author}: {text}"
        if file_path and file_path.exists():
            url = await upload_to_xmpp(file_path)
            if url:
                m_id = send_xmpp_media(xmpp_client, xmpp_room, author, url, text, reply_targets.get('xmpp'))
                if m_id: sent_mappings['xmpp'] = m_id
            else:
                m_id = send_xmpp_text(xmpp_client, xmpp_room, f"{author} enviou mídia (falha upload)", None, reply_targets.get('xmpp'))
                if m_id: sent_mappings['xmpp'] = m_id
        else:
            m_id = send_xmpp_text(xmpp_client, xmpp_room, xmpp_text, None, reply_targets.get('xmpp'))
            if m_id: sent_mappings['xmpp'] = m_id

    register_message('discord', message.id, sent_mappings, author, text)
    if file_path and file_path.exists(): file_path.unlink()

@discord_client.event
async def on_message_edit(before, after):
    if after.author.bot: return
    bridge = discord_to_bridge.get(after.channel.id)
    if not bridge: return
    targets = get_reply_targets(before.id)
    if not targets: return

    for room_id in [bridge.get('matrix_room')] if bridge.get('matrix_room') else []:
        if targets.get('matrix'): await send_matrix_edit(room_id, targets['matrix'], {"msgtype": "m.text", "body": f"{after.author.display_name}: {after.content}"})
    for tg_id in bridge.get('telegram_chats', []):
        if targets.get('telegram'): await send_telegram_edit(tg_id, targets['telegram'], f"<b>{escape_html(after.author.display_name)}:</b> {escape_html(after.content)}")
    for xmpp_room in bridge.get('xmpp_rooms', []):
        if targets.get('xmpp'): send_xmpp_edit(xmpp_client, xmpp_room, f"{after.author.display_name}: {after.content}", targets['xmpp'])

@discord_client.event
async def on_message_delete(message):
    if message.author.bot: return
    bridge = discord_to_bridge.get(message.channel.id)
    if not bridge: return
    targets = get_reply_targets(message.id)
    if not targets: return

    for room_id in [bridge.get('matrix_room')] if bridge.get('matrix_room') else []:
        if targets.get('matrix') and matrix_client:
            try: await matrix_client.room_redact(room_id, targets['matrix'])
            except Exception: pass
    for tg_id in bridge.get('telegram_chats', []):
        if targets.get('telegram'):
            try: await telegram_bot.delete_message(chat_id=tg_id, message_id=targets['telegram'])
            except Exception: pass

@discord_client.event
async def on_ready():
    print(f"✅ [Discord] Logado como {discord_client.user} - Pronto para mensagens")

async def telegram_message_handler(update: Update, context):
    if not update.message or not update.message.chat: return
    chat_id = update.message.chat.id
    bridge = telegram_to_bridge.get(chat_id)
    if not bridge: return

    author = update.message.from_user.full_name
    text = update.message.text
    caption = update.message.caption

    reply_targets = {}
    if update.message.reply_to_message:
        reply_targets = get_reply_targets(update.message.reply_to_message.message_id)

    fallback_quote = get_fallback_quote(reply_targets)
    file_path, filename, content_type = None, None, None

    if update.message.photo:
        file_id = update.message.photo[-1].file_id; filename = f"photo_{file_id}.jpg"; content_type = "image/jpeg"
    elif update.message.video:
        file_id = update.message.video.file_id; filename = get_safe_filename(update.message.video.file_name or f"video_{file_id}.mp4", update.message.video.mime_type); content_type = update.message.video.mime_type
    elif update.message.audio:
        file_id = update.message.audio.file_id; filename = get_safe_filename(update.message.audio.file_name or f"audio_{file_id}.mp3", update.message.audio.mime_type); content_type = update.message.audio.mime_type
    elif update.message.document:
        file_id = update.message.document.file_id; filename = get_safe_filename(update.message.document.file_name or f"doc_{file_id}", update.message.document.mime_type); content_type = update.message.document.mime_type
    else: file_id = None

    if file_id:
        tg_file = await context.bot.get_file(file_id)
        file_path = TEMP_DIR / f"telegram_{update.message.message_id}_{filename}"
        await tg_file.download_to_drive(file_path)

    body = caption if caption else text or ''
    sent_mappings = {}

    for room_id in [bridge.get('matrix_room')] if bridge.get('matrix_room') else []:
        if not matrix_client: continue
        content = {"msgtype": "m.text", "body": f"{author}: {body}"} if body else {"msgtype": "m.text", "body": f"{author} enviou mídia"}
        if reply_targets.get('matrix'): content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_targets['matrix']}}
        if file_path and file_path.exists():
            uri = await upload_to_matrix(file_path, filename, content_type)
            if uri:
                content['msgtype'] = get_media_type(content_type)
                content['url'] = uri
                content['body'] = filename
        try:
            resp = await matrix_client.room_send(room_id, "m.room.message", content)
            if isinstance(resp, RoomSendResponse): sent_mappings['matrix'] = resp.event_id
        except Exception: pass

    for ch_id in bridge.get('discord_channels', []):
        discord_text = f"{fallback_quote}**{author}:** {body}" if body else f"{fallback_quote}**{author}** enviou mídia"
        try:
            if file_path and file_path.exists():
                with open(file_path, 'rb') as f:
                    sent = await send_discord_webhook_message(ch_id, author, None, discord_text, DiscordFile(f, filename=filename))
            else:
                sent = await send_discord_webhook_message(ch_id, author, None, discord_text)
            if sent: sent_mappings['discord'] = sent.id
        except Exception: pass

    for xmpp_room in bridge.get('xmpp_rooms', []):
        xmpp_text = f"{fallback_quote}{author}: {body}"
        if file_path and file_path.exists():
            url = await upload_to_xmpp(file_path)
            if url:
                m_id = send_xmpp_media(xmpp_client, xmpp_room, author, url, body, reply_targets.get('xmpp'))
                if m_id: sent_mappings['xmpp'] = m_id
            else:
                m_id = send_xmpp_text(xmpp_client, xmpp_room, f"{author} enviou mídia (falha upload)", None, reply_targets.get('xmpp'))
                if m_id: sent_mappings['xmpp'] = m_id
        else:
            m_id = send_xmpp_text(xmpp_client, xmpp_room, xmpp_text, None, reply_targets.get('xmpp'))
            if m_id: sent_mappings['xmpp'] = m_id

    register_message('telegram', update.message.message_id, sent_mappings, author, body)
    if file_path and file_path.exists(): file_path.unlink()

telegram_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, telegram_message_handler))

class BridgeXMPPClient(slixmpp.ClientXMPP):
    def __init__(self, jid, password):
        super().__init__(jid, password)
        self.register_plugin('xep_0030')
        self.register_plugin('xep_0045') # MUC
        self.register_plugin('xep_0199') # Ping
        self.register_plugin('xep_0363') # HTTP Upload
        self.register_plugin('xep_0359') # Stanza IDs
        self.add_event_handler("session_start", self.start)
        self.add_event_handler("groupchat_message", self.muc_message)

    async def start(self, event):
        self.send_presence()
        await self.get_roster()
        print(f"✅ [XMPP] Logado com sucesso como {self.boundjid.bare}")
        for room in xmpp_to_bridge:
            try:
                await asyncio.to_thread(self.plugin['xep_0045'].join_muc, room, self.boundjid.user)
                print(f"✅ [XMPP] Entrou na sala MUC: {room}")
            except Exception as e: print(f"❌ [XMPP] Erro MUC {room}: {e}")

    async def muc_message(self, msg):
        if msg['mucnick'] == self.boundjid.user: return
        room = msg['from'].bare
        bridge = xmpp_to_bridge.get(room)
        if not bridge: return

        sender = msg['mucnick']
        body = msg['body']
        replace = msg.xml.find('{urn:xmpp:message-correct:0}replace')
        if replace is not None: return

        reply_id = None
        reply_elem = msg.xml.find('{urn:xmpp:reply:0}reply')
        if reply_elem is not None: reply_id = reply_elem.get('id')
        reply_targets = get_reply_targets(reply_id)
        fallback_quote = get_fallback_quote(reply_targets)

        file_path, filename, content_type = None, None, None
        oob = msg.xml.find('{jabber:x:oob}x/{jabber:x:oob}url')
        url = oob.text if oob is not None else None

        if not url:
            for word in body.split():
                if word.startswith(('http://', 'https://')):
                    dl_path, dl_name, dl_type = await fetch_xmpp_media(word)
                    if dl_path:
                        url, file_path, filename, content_type = word, Path(dl_path), dl_name, dl_type
                        body = body.replace(word, '').strip()
                        break

        sent_mappings = {}

        for room_id in [bridge.get('matrix_room')] if bridge.get('matrix_room') else []:
            if not matrix_client: continue
            content = {"msgtype": "m.text", "body": f"{sender}: {body}"} if body else {"msgtype": "m.text", "body": f"{sender} enviou mídia"}
            if reply_targets.get('matrix'): content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_targets['matrix']}}
            if file_path and file_path.exists():
                uri = await upload_to_matrix(file_path, filename, content_type)
                if uri:
                    content['msgtype'] = get_media_type(content_type)
                    content['url'] = uri
                    content['body'] = filename
            try:
                resp = await matrix_client.room_send(room_id, "m.room.message", content)
                if isinstance(resp, RoomSendResponse): sent_mappings['matrix'] = resp.event_id
            except Exception: pass

        for ch_id in bridge.get('discord_channels', []):
            discord_text = f"{fallback_quote}**{sender}:** {body}" if body else f"{fallback_quote}**{sender}** enviou mídia"
            try:
                if file_path and file_path.exists():
                    with open(file_path, 'rb') as f:
                        sent = await send_discord_webhook_message(ch_id, sender, None, discord_text, DiscordFile(f, filename=filename))
                else:
                    sent = await send_discord_webhook_message(ch_id, sender, None, discord_text)
                if sent: sent_mappings['discord'] = sent.id
            except Exception: pass

        for tg_id in bridge.get('telegram_chats', []):
            try:
                tg_text = f"<b>{escape_html(sender)}:</b> {escape_html(body)}" if body else f"<b>{escape_html(sender)}</b> enviou mídia"
                kwargs = {}
                if reply_targets.get('telegram'): kwargs['reply_to_message_id'] = reply_targets['telegram']
                if file_path and file_path.exists():
                    with open(file_path, 'rb') as f:
                        method = get_telegram_media_method(content_type)
                        if method == 'send_photo': sent = await telegram_bot.send_photo(chat_id=tg_id, photo=f, caption=tg_text, parse_mode=ParseMode.HTML, **kwargs)
                        elif method == 'send_video': sent = await telegram_bot.send_video(chat_id=tg_id, video=f, caption=tg_text, parse_mode=ParseMode.HTML, **kwargs)
                        elif method == 'send_audio': sent = await telegram_bot.send_audio(chat_id=tg_id, audio=f, caption=tg_text, parse_mode=ParseMode.HTML, **kwargs)
                        else: sent = await telegram_bot.send_document(chat_id=tg_id, document=f, caption=tg_text, parse_mode=ParseMode.HTML, **kwargs)
                else:
                    sent = await telegram_bot.send_message(chat_id=tg_id, text=tg_text, parse_mode=ParseMode.HTML, **kwargs)
                if sent: sent_mappings['telegram'] = sent.message_id
            except Exception: pass

        register_message('xmpp', msg['id'], sent_mappings, sender, body)
        if file_path and file_path.exists(): file_path.unlink()

# ================== START GERAL ==================

async def sync_matrix():
    state = load_state()
    sync_token = state.get('sync_token')
    matrix_client.add_event_callback(matrix_message_callback, RoomMessage)
    matrix_client.add_event_callback(handle_matrix_redaction, RedactionEvent)

    while True:
        try:
            res = await matrix_client.sync(sync_filter={"room": {"timeline": {"limit": 10}}}, since=sync_token, full_state=True)
            sync_token = res.next_batch
            state['sync_token'] = sync_token
            save_state(state)
        except Exception as e:
            print(f"Erro no sync Matrix: {e}")
            await asyncio.sleep(5)

async def main():
    global matrix_client, xmpp_client
    matrix_client = await matrix_login()

    xmpp_client = BridgeXMPPClient(XMPP_JID, XMPP_PASSWORD)
    xmpp_client.connect((XMPP_SERVER, XMPP_PORT))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    print("✅ [Telegram] Bot conectado e escutando mensagens")

    await asyncio.gather(
        discord_client.start(DISCORD_TOKEN),
        sync_matrix() if matrix_client else asyncio.sleep(0)
    )

if __name__ == "__main__":
    asyncio.run(main())
