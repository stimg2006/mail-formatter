import io
import os
import re
import tempfile
import zipfile

import extract_msg
import streamlit as st
from bs4 import BeautifulSoup

# ── テキスト処理ロジック ─────────────────────────────────────────────────────

_SEPARATOR_RE = re.compile(r'^[ \t]*[-_=*]{5,}[^\n]*$\r?\n?', re.MULTILINE)
_REPLY_HEADER_RE = re.compile(
    r'^[ \t]*(From|Sent|To|Cc|Bcc|Subject|Date|差出人|送信日時|宛先|件名|CC|BCC)\s*:.*$\r?\n?',
    re.MULTILINE | re.IGNORECASE,
)
_SIG_INDICATOR_RE = re.compile(
    r'\S+@\S+<mailto:'
    r'|^[ \t]*(Mobile|Phone|Tel|Fax|電話|携帯)[\s:+]',
    re.IGNORECASE | re.MULTILINE,
)
_CLOSING_RE = re.compile(
    r'\b(regards|sincerely|thanks|thank\s+you|best|cheers|'
    r'よろしく|以上|敬具|失礼します)\b',
    re.IGNORECASE,
)


def _is_short_block(text: str) -> bool:
    lines = [l for l in text.splitlines() if l.strip()]
    return bool(lines) and len(lines) <= 5 and all(len(l.strip()) <= 55 for l in lines)


def strip_reply_headers(text: str) -> str:
    text = _SEPARATOR_RE.sub('', text)
    text = _REPLY_HEADER_RE.sub('', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def strip_signatures(text: str) -> str:
    paragraphs = re.split(r'\n\n+', text.strip())
    to_remove: set[int] = set()
    for i, para in enumerate(paragraphs):
        if _SIG_INDICATOR_RE.search(para):
            to_remove.add(i)
            j = i - 1
            while j >= 0 and _is_short_block(paragraphs[j]):
                if _CLOSING_RE.search(paragraphs[j]):
                    break
                to_remove.add(j)
                j -= 1
    result = [p for idx, p in enumerate(paragraphs) if idx not in to_remove]
    return re.sub(r'\n{3,}', '\n\n', '\n\n'.join(result)).strip()


def extract_body(msg_path: str) -> str:
    for enc in ("cp932", "utf-8", "latin-1"):
        try:
            msg = extract_msg.Message(msg_path, overrideEncoding=enc)
            try:
                body = msg.body
                if body and body.strip():
                    body = body.replace('\r\n', '\n').replace('\r', '\n')
                    return strip_signatures(strip_reply_headers(body))
                html = msg.htmlBody
                if html:
                    soup = BeautifulSoup(html, "html.parser")
                    text = soup.get_text(separator="\n").replace('\r\n', '\n').replace('\r', '\n')
                    return strip_signatures(strip_reply_headers(text))
                return ""
            finally:
                msg.close()
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return ""


# ── Streamlit UI ─────────────────────────────────────────────────────────────

st.set_page_config(page_title="メール整形ツール", page_icon="✉️", layout="centered")
st.title("✉️ メール整形ツール")
st.caption("返信ヘッダーと署名を除去してクリーンな本文を取り出します")

tab1, tab2 = st.tabs(["📁 ファイル一括処理（.msg）", "📋 テキスト貼り付け"])

# ── タブ1: .msg ファイルアップロード ──────────────────────────────────────────
with tab1:
    st.markdown("Outlookの `.msg` ファイルを複数アップロードして、整形済みテキストをZIPで受け取ります。")
    uploaded = st.file_uploader(
        ".msg ファイルを選択（複数可）",
        type=["msg"],
        accept_multiple_files=True,
    )

    if st.button("整形実行", key="btn_file", type="primary") and uploaded:
        zip_buf = io.BytesIO()
        log_placeholder = st.empty()
        logs: list[str] = []

        with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in uploaded:
                with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as tmp:
                    tmp.write(f.read())
                    tmp_path = tmp.name
                try:
                    body = extract_body(tmp_path)
                    out_name = os.path.splitext(f.name)[0] + ".txt"
                    zf.writestr(out_name, body)
                    logs.append(f"✅ {f.name}")
                except Exception as e:
                    logs.append(f"❌ {f.name} — {e}")
                finally:
                    os.unlink(tmp_path)
                log_placeholder.text("\n".join(logs))

        zip_buf.seek(0)
        st.success(f"{len(uploaded)} 件処理完了")
        st.download_button(
            label="📥 ZIP でダウンロード",
            data=zip_buf,
            file_name="output.zip",
            mime="application/zip",
        )

# ── タブ2: テキスト貼り付け ───────────────────────────────────────────────────
with tab2:
    st.markdown("任意のメール本文を貼り付けると、返信ヘッダーと署名を除去して返します。")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("入力")
        raw = st.text_area(
            "ここに貼り付け",
            height=380,
            placeholder="メール本文をここに貼り付けてください…",
            label_visibility="collapsed",
        )

    with col2:
        st.subheader("整形後")
        if st.button("整形", key="btn_paste", type="primary") and raw.strip():
            result = strip_signatures(strip_reply_headers(raw))
            st.session_state["paste_result"] = result

        result_text = st.session_state.get("paste_result", "")
        st.text_area(
            "整形結果",
            value=result_text,
            height=380,
            label_visibility="collapsed",
        )
