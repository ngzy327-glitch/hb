import asyncio
import re
import time
import json
import urllib.request
import urllib.parse
import urllib.error
import hashlib
import base64
from datetime import datetime
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest
from telethon.tl.types import MessageEntityCustomEmoji

# ========== 全局配置 ==========
API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
BOT_TOKEN = '你的通知BotToken'      # ⚠️ 改成你自己的
MY_CHAT_ID = 123456789              # ⚠️ 改成你自己的TG ID
TARGET_CHAT_IDS = [-1002803069645]  # ⚠️ 改成你要监听的群ID

# 【视觉 AI 配置】
VISION_API_KEY = "sk-ws-H.PDPRPRL.L9t1.MEYCIQDIn-4_Fl6P6II8jXeJEgp3SvLVDtW4Elqj65D1ZX-xFwIhAJvZUEEZJr5B83kaK0UJgnTsWNXrsHcJwoqBzxZnGhtA"
VISION_BASE_URL = "https://maas.qianwenaiapi.com/compatible-mode/v1"
VISION_MODEL = "qwen3.7-plus"  # ⚠️ 如报404，去平台后台确认模型名

BLOCKED_SENDERS = ['dlqb', 'nmqg', 'shadan', 'gouer']
BLOCKED_MSG_KEYWORDS = ['测挂']
BUTTON_KEYWORDS = ['领取', '抢', '领红包', '领取红包', '提交', '确认']
CLICK_INTERVAL = 2.0
# ============================

# ========== 本地内存缓存（替代 Redis + PostgreSQL） ==========
EMOJI_HASH_CACHE = {}     # thumb_md5 -> 字符（跨包共享，最稳定的键）
EMOJI_DOC_CACHE = {}      # doc_id -> 字符（同消息的快路径）
EMOJI_DOCHASH_CACHE = {}  # doc_id -> thumb_md5（纠错反查用）
CACHE_LOCK = asyncio.Lock()

# 普通 Unicode Emoji 数字映射（如 1️⃣、①、一）
EMOJI_NUM_MAP = {
    '1️⃣': '1', '2️⃣': '2', '3️⃣': '3', '4️⃣': '4', '5️⃣': '5', '6️⃣': '6', '7️⃣': '7', '8️⃣': '8', '9️⃣': '9', '0️⃣': '0',
    '1⃣': '1', '2⃣': '2', '3⃣': '3', '4⃣': '4', '5⃣': '5', '6⃣': '6', '7⃣': '7', '8⃣': '8', '9⃣': '9', '0⃣': '0',
    '①': '1', '②': '2', '③': '3', '④': '4', '⑤': '5', '⑥': '6', '⑦': '7', '⑧': '8', '⑨': '9', '⓪': '0',
    '一': '1', '二': '2', '三': '3', '四': '4', '五': '5', '六': '6', '七': '7', '八': '8', '九': '9', '零': '0'
}

def clean_text(text):
    if not text: return ""
    return re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00a0\n\r]', '', text).strip()

def normalize_emoji_numbers(text):
    if not text: return ""
    for emoji, num in EMOJI_NUM_MAP.items():
        text = text.replace(emoji, num)
    return text

# 【终极修复】改用 GET 请求 + URL 强制编码，彻底解决 iOS 的 ascii 编码报错
async def send_tg_log(message):
    safe_text = urllib.parse.quote(message, encoding='utf-8')
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage?chat_id={MY_CHAT_ID}&text={safe_text}"
    req = urllib.request.Request(url, method='GET')
    try:
        def _send():
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        await asyncio.to_thread(_send)
    except Exception as e:
        print(f"[通知发送失败] {e}")

# ========== 自定义 Emoji 解码器（移植自 emoji_decoder.py） ==========
async def _ai_identify(thumb_bytes: bytes, want_digit: bool | None = None) -> str:
    if not thumb_bytes or not VISION_API_KEY:
        return ""
    if want_digit is True:
        prompt = "图片上是一个阿拉伯数字（0到9之间）。只回答这一个数字，不要任何其他内容。"
    elif want_digit is False:
        prompt = "图片上是一个英文大写字母（A到Z之间）。只回答这一个字母，不要任何其他内容。"
    else:
        prompt = "图片上是什么字母或数字？只回答一个字符。"
    
    b64 = base64.b64encode(thumb_bytes).decode()
    url = f"{VISION_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {VISION_API_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "model": VISION_MODEL,
        "max_tokens": 10,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(data, ensure_ascii=True).encode('utf-8'), headers=headers, method='POST')
        def _do_request():
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read().decode('utf-8')
        
        # 强制最多等 20 秒，超时直接放弃，不再卡死整个脚本
        resp_text = await asyncio.wait_for(asyncio.to_thread(_do_request), timeout=20.0)
        result = json.loads(resp_text)
        answer = result["choices"][0]["message"]["content"].strip().upper()
        if want_digit is True: ch = "".join(c for c in answer if c.isdigit())
        elif want_digit is False: ch = "".join(c for c in answer if c.isascii() and c.isalpha())
        else: ch = "".join(c for c in answer if c.isascii() and c.isalnum())
        return ch if ch and len(ch) <= 2 else ""
    except asyncio.TimeoutError:
        print("[AI识别超时] 已放弃该表情，继续处理下一条消息。")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8', errors='ignore')
        print(f"[AI识别报错] HTTP {e.code}: {error_body[:200]}")
    except Exception as e:
        print(f"[AI识别网络异常] {e}")
    return ""

async def _download_thumb(client, doc):
    if not doc.thumbs:
        return doc.id, None, None
    try:
        thumb = await client.download_media(doc, file=bytes, thumb=-1)
        if thumb:
            return doc.id, hashlib.md5(thumb).hexdigest(), thumb
    except Exception as e:
        print(f"[缩略图下载失败] {doc.id}: {e}")
    return doc.id, None, None

async def decode_custom_emoji(client, doc_ids: list[int], want_digit: bool | None = None) -> dict[int, str]:
    if not doc_ids:
        return {}

    result, missing = {}, []
    async with CACHE_LOCK:
        for did in doc_ids:
            if did in EMOJI_DOC_CACHE:
                result[did] = EMOJI_DOC_CACHE[did]
            else:
                missing.append(did)
    if not missing:
        return result

    try:
        docs = await client(GetCustomEmojiDocumentsRequest(document_id=missing))
    except Exception as e:
        print(f"[获取Emoji文档失败] {e}")
        return result
    
    thumbs = await asyncio.gather(*[_download_thumb(client, d) for d in docs])
    doc_hash, hash_bytes = {}, {}
    async with CACHE_LOCK:
        for did, h, b in thumbs:
            if h:
                doc_hash[did] = h
                hash_bytes[h] = b
                EMOJI_DOCHASH_CACHE[did] = h

    ai_hashes = []
    async with CACHE_LOCK:
        for h in hash_bytes:
            if h not in EMOJI_HASH_CACHE:
                ai_hashes.append(h)

    if ai_hashes:
        ai_results = await asyncio.gather(*[_ai_identify(hash_bytes[h], want_digit) for h in ai_hashes])
        async with CACHE_LOCK:
            for h, ch in zip(ai_hashes, ai_results):
                if ch:
                    EMOJI_HASH_CACHE[h] = ch

    async with CACHE_LOCK:
        for did, h in doc_hash.items():
            ch = EMOJI_HASH_CACHE.get(h)
            if ch:
                result[did] = ch
                EMOJI_DOC_CACHE[did] = ch

    return result

def extract_custom_emoji_ids(msg) -> list[tuple[int, int, int]]:
    entities = getattr(msg, "entities", None) or []
    return [(e.offset, e.length, e.document_id) for e in entities if isinstance(e, MessageEntityCustomEmoji)]

def extract_emojis(text):
    if not text: return []
    emoji_pattern = re.compile(
        "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251"
        "\U0001f926-\U0001f937\U00010000-\U0010ffff\u2640-\u2642"
        "\u2600-\u2B55\u23cf\u23e9\u231a\ufe0f\u3030]+", flags=re.UNICODE)
    return emoji_pattern.findall(text)

def calculate_math_expression(text):
    if not text: return None
    t = normalize_emoji_numbers(text).lower()
    t = t.replace('加', '+').replace('减', '-').replace('乘', '*').replace('除', '/')
    t = t.replace('＋', '+').replace('－', '-').replace('×', '*').replace('÷', '/')
    match = re.search(r'(\d+)\s*([\+\-\*xX\/])\s*(\d+)', t)
    if not match: return None
    num1, op, num2 = int(match.group(1)), match.group(2), int(match return.group(3))
    if op == any '+': return(k str(num1 + num2)
    if in op == '-': return str(num1 - num2)
    if op in ['*', 'x']: return str(num1 * num2)
    if op == '/': return str(round(num1 / num2, 2)) if num1 % num2 != 0 else str(num1 // num2)

def is_target_button(button_text):
    cleaned = clean_text(button_text)
    cleaned for k in BUTTON_KEYWORDS)

# ========== 消息处理核心 ==========
async def process_message(event, acc_name, clicked_set):
    if not event.is_group or event.chat_id not in TARGET_CHAT_IDS: return
    if BLOCKED_SENDERS:
        sender = await event.get_sender()
        sender_username = getattr(sender, 'username', None) if sender else None
        if sender_username and sender_username.lower() in [s.lower() for s in BLOCKED_SENDERS]:
            return 

    msg_id = event.message.id
    chat_id = event.chat_id
    if (chat_id, msg_id) in clicked_set: return
    if len(clicked_set) > 500: clicked_set.clear()

    print(f"\n[{acc_name}] 监听中 消息ID: {msg_id}")

    try:
        msg = await event.client.get_messages(chat_id, ids=msg_id)
        if not msg: return
        try:
            chat = await event.get_chat()
            chat_title = getattr(chat, 'title', '未知群组')
            raw_id = str(chat_id).replace('-100', '')
            msg_link = f"https://t.me/c/{raw_id}/{msg_id}"
        except Exception:
            chat_title = "未知群组"; msg_link = "无法获取链接"

        raw_text_cleaned = clean_text(msg.raw_text)
        if any(k in raw_text_cleaned for k in BLOCKED_MSG_KEYWORDS): return

        markup = msg.reply_markup
        if not markup or not hasattr(markup, 'rows'): return

        buttons = []
        for i, row in enumerate(markup.rows):
            for j, button in enumerate(row.buttons):
                btn_text = getattr(button, 'text', '') or ''
                buttons.append((i, j, button, btn_text))

        # ========== 自定义 Emoji 解码介入 ==========
        custom_emoji_ids = []
        for _, _, did in extract_custom_emoji_ids(msg):
            custom_emoji_ids.append(did)
        for _, _, button, btn_text in buttons:
            btn_entities = getattr(button, 'entities', None)
            if btn_entities:
                for e in btn_entities:
                    if isinstance(e, MessageEntityCustomEmoji):
                        custom_emoji_ids.append(e.document_id)
        custom_emoji_ids = list(set(custom_emoji_ids))

        decoded_emojis = {}
        if custom_emoji_ids:
            print(f"     [Emoji解码] 正在解码 {len(custom_emoji_ids)} 个自定义表情...")
            decoded_emojis = await decode_custom_emoji(event.client, custom_emoji_ids, want_digit=True)
        # ====================================

        # 1. 普通 Unicode Emoji 验证
        msg_emojis = extract_emojis(msg.raw_text)
        if msg_emojis:
            for i, j, button, btn_text in buttons:
                if any(emoji in clean_text(btn_text) for emoji in msg_emojis):
                    if hasattr(button, 'url') and button.url: continue
                    try:
                        await msg.click(i=i, j=j)
                        await send_tg_log(f"[{acc_name}] 🎭 Emoji验证通过\n群：{chat_title}\n链接：{msg_link}")
                        clicked_set.add((chat_id, msg_id))
                        await asyncio.sleep(CLICK_INTERVAL)
                        return
                    except Exception: pass

        # 2. 算术验证
        math_answer = calculate_math_expression(raw_text_cleaned)
        if math_answer:
            for i, j, button, btn_text in buttons:
                normalized = clean_text(btn_text)
                if normalized == math_answer or decoded_emojis.get(getattr(button, 'document_id', None)) == math_answer:
                    if hasattr(button, 'url') and button.url: continue
                    try:
                        await msg.click(i=i, j=j)
                        await send_tg_log(f"[{acc_name}] 🧮 验证通过\n群：{chat_title}\n链接：{msg_link}")
                        clicked_set.add((chat_id, msg_id))
                        await asyncio.sleep(CLICK_INTERVAL)
                        return
                    except Exception: pass

        # 3. 常规红包点击
        has_keyword = any(k in raw_text_cleaned for k in ['领取红包', '🧧', '领取', '红包'])
        for i, j, button, btn_text in buttons:
            btn_cleaned = clean_text(btn_text)
            
            # 情况A：普通的"领取红包"按钮
            if is_target_button(btn_text) or (has_keyword and len(btn_cleaned) < 20):
                if hasattr(button, 'url') and button.url: return
                print(f"     -> 发现红包按钮 '{btn_cleaned}'，点击中...")
                try:
                    response = await msg.click(i=i, j=j)
                    answer = getattr(response, 'message', None)
                    if answer and not any(k in answer for k in ["领完", "手慢", "失败", "已抢", "抢光"]):
                        await send_tg_log(f"[{acc_name}] ✅ 成功领取！\n回复：{answer}\n群：{chat_title}\n链接：{msg_link}")
                    elif not answer:
                        await send_tg_log(f"[{acc_name}] ✅ 成功点击！\n群：{chat_title}\n链接：{msg_link}")
                    clicked_set.add((chat_id, msg_id))
                    await asyncio.sleep(CLICK_INTERVAL)
                    return
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    print(f"     [点击异常] {e}")
                    return

            # 情况B：按钮是自定义表情，且与消息中的自定义表情匹配
            if decoded_emojis:
                btn_doc_id = getattr(button, 'document_id', None)
                decoded_chars = set(decoded_emojis.values())
                btn_chars = set(btn_cleaned)
                if btn_chars & decoded_chars or (btn_doc_id and decoded_emojis.get(btn_doc_id)):
                    if hasattr(button, 'url') and button.url: continue
                    print(f"     -> [Emoji匹配] 按钮 '{btn_cleaned}' 与目标表情匹配，点击中...")
                    try:
                        await msg.click(i=i, j=j)
                        await send_tg_log(f"[{acc_name}] 🎭 Emoji验证通过\n群：{chat_title}\n链接：{msg_link}")
                        clicked_set.add((chat_id, msg_id))
                        await asyncio.sleep(CLICK_INTERVAL)
                        return
                    except Exception as e:
                        print(f"     [Emoji点击异常] {e}")
    except Exception as e:
        print(f"[{acc_name}] 检测出错: {e}")

async def main():
    try:
        with open('accounts.json', 'r', encoding='utf-8') as f:
            accounts = json.load(f)
    except Exception as e:
        print(f"❌ 读取 accounts.json 失败: {e}"); return

    if not accounts:
        print("❌ accounts.json 为空"); return

    acc = accounts[0]
    phone = acc['phone']
    session_name = acc['session']
    
    client = TelegramClient(session_name, API_ID, API_HASH)
    await client.start(phone=phone)
    print(f"✅ 账号 {phone} ({session_name}) 已登录，开始监听...")

    clicked_set = set()
    async def handler(event):
        await process_message(event, session_name, clicked_set)
        
    client.add_event_handler(handler, events.NewMessage(incoming=True))
    client.add_event_handler(handler, events.MessageEdited(incoming=True))
    
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())