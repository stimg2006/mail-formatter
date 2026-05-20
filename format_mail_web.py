import io
import os
import re
import tempfile
import zipfile

import extract_msg
import streamlit as st
import streamlit.components.v1 as components
from bs4 import BeautifulSoup
from PIL import Image

# ── OCR (Tesseract) 設定 ─────────────────────────────────────────────────────
# 日本語＋英語の混在テキストに対応。ローカル(Windows)では tesseract.exe を自動検出する。
try:
    import pytesseract  # type: ignore

    if os.name == "nt":
        for _candidate in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ):
            if os.path.exists(_candidate):
                pytesseract.pytesseract.tesseract_cmd = _candidate
                break
    _OCR_AVAILABLE = True
except Exception:  # pytesseract 自体が無い場合
    pytesseract = None  # type: ignore
    _OCR_AVAILABLE = False

_OCR_LANG = "jpn+eng"
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp")

# ── テキスト処理ロジック ─────────────────────────────────────────────────────

_SEPARATOR_RE = re.compile(r'^[ \t]*[-_=*]{5,}[^\n]*$\r?\n?', re.MULTILINE)

# メール境界を示す区切り線（From: 行の前に挿入）
SEPARATOR_LINE = '=' * 52

# 削除するヘッダー行: From / Sent / Date / 差出人 / 送信日時 / 送信日 は残す
_REMOVE_HEADER_RE = re.compile(
    r'^[ \t]*(To|Cc|Bcc|Subject|Importance|Priority|Sensitivity|'
    r'宛先|件名|CC|BCC|重要度|優先度)\s*:.*$\r?\n?',
    re.MULTILINE | re.IGNORECASE,
)

# 差出人ヘッダー行（前に区切り線を挿入する目印）
_FROM_LINE_RE = re.compile(
    r'^[ \t]*(?:From|差出人)\s*:',
    re.MULTILINE | re.IGNORECASE,
)

# HTML メール由来でラベル行と値行が改行で分離されているケースを 1 行に折り畳む。
# 例)  "From:\nSyed Yusuff Basha ..."  →  "From: Syed Yusuff Basha ..."
_FOLD_HEADER_RE = re.compile(
    r'^([ \t]*(?:From|Sent|Date|To|Cc|Bcc|Subject|Importance|Priority|Sensitivity|'
    r'差出人|送信日時|送信日|宛先|件名|CC|BCC|重要度|優先度)[ \t]*:)[ \t]*\n+(?=[ \t]*\S)',
    re.MULTILINE | re.IGNORECASE,
)
_SIG_INDICATOR_RE = re.compile(
    r'\S+@\S+<mailto:'                          # HTML形式 email<mailto:...>
    r'|\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b'         # 素のメールアドレス
    r'|^[ \t]*(Mobile|Phone|Tel|Fax|電話|携帯)[\s:+]'  # ラベル付き電話番号
    r'|^\s*\+?[\d][\d\s\-\(\)\.]{6,}\s*$',     # ラベルなし電話番号（+1-734-855-3244 等）
    re.IGNORECASE | re.MULTILINE,
)
_CLOSING_RE = re.compile(
    r'\b(regards|sincerely|thanks|thank\s+you|best|cheers|'
    r'よろしく|以上|敬具|失礼します)\b',
    re.IGNORECASE,
)

# 結び言葉から始まる行（独立した署名ブロックの判定に使う）
# "Thanks for the update." のような本文と区別するため、汎用的な単語 (thanks / thank you / best)
# の直後に句読点または行末があることを必須にする。
_SIG_OPENER_RE = re.compile(
    r'^\s*(?:'
    r'kind\s+regards|best\s+regards|warm\s+regards|with\s+regards|'
    r'best\s+wishes|thanks\s+and\s+regards|thanks\s+&\s+regards|'
    r'regards|sincerely|cheers|'
    r'thanks(?:\s+(?:in\s+advance|a\s+lot|so\s+much|again))?(?=[,，、。.!]|\s*$)|'
    r'thank\s+you(?:\s+very\s+much)?(?=[,，、。.!]|\s*$)|'
    r'best(?=[,，、。.!]|\s*$)|'
    r'よろしく(?:お願い(?:いた)?します)?|以上(?:です)?|敬具|失礼(?:いた)?します'
    r')'
    r'[,，、。.\s!]',
    re.IGNORECASE | re.MULTILINE,
)


def _is_short_block(text: str) -> bool:
    lines = [l for l in text.splitlines() if l.strip()]
    return bool(lines) and len(lines) <= 5 and all(len(l.strip()) <= 55 for l in lines)


def strip_reply_headers(text: str) -> str:
    # 既存のダッシュ等の区切り線を一旦削除
    text = _SEPARATOR_RE.sub('', text)
    # HTML由来でラベルと値が改行分離されている場合、1行に折り畳んでから削除判定にかける
    text = _FOLD_HEADER_RE.sub(r'\1 ', text)
    # 不要なヘッダー行を削除（From / Sent / Date / 差出人 / 送信日時 は残す）
    text = _REMOVE_HEADER_RE.sub('', text)
    # From: / 差出人: 行の前に区切り線を挿入（残るのは From: / Sent: 等のみ）
    text = _FROM_LINE_RE.sub(SEPARATOR_LINE + '\n' + r'\g<0>', text)
    # 空白のみの行を空行に、連続する空行を1行に
    text = re.sub(r'^[ \t]+$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    # 連続する区切り線を 1 本にまとめる
    sep_pat = re.escape(SEPARATOR_LINE)
    text = re.sub(rf'({sep_pat})(?:\s*\n+\s*{sep_pat})+', r'\1', text)
    # 冒頭の区切り線（前にメールが存在しないので不要）を除去
    text = re.sub(rf'\A\s*{sep_pat}\s*\n', '', text)
    return text.strip()


def _looks_like_signature_block(para: str) -> bool:
    """結び言葉から始まる短いブロックを独立した署名とみなす。"""
    if not _SIG_OPENER_RE.match(para):
        return False
    lines = [l for l in para.splitlines() if l.strip()]
    if not lines:
        return False
    # 結び言葉から始まり、最大 12 行 / 各行 ≤80 文字 のブロックを署名候補とする
    return len(lines) <= 12 and all(len(l.strip()) <= 80 for l in lines)


def _strip_inline_signature(para: str) -> str:
    """段落の途中から結び言葉＋短い行が続く場合、そこから末尾までを署名として除去。"""
    lines = para.splitlines()
    for idx, line in enumerate(lines):
        if idx == 0:
            continue  # 段落冒頭は _looks_like_signature_block で処理済み
        if _SIG_OPENER_RE.match(line):
            tail = lines[idx:]
            non_empty_tail = [l for l in tail if l.strip()]
            if non_empty_tail and len(non_empty_tail) <= 12 and \
                    all(len(l.strip()) <= 80 for l in non_empty_tail):
                return "\n".join(lines[:idx]).rstrip()
    return para


def strip_signatures(text: str) -> str:
    # 空白文字だけの行（"\n \n" など）を空行に正規化してから分割
    text = re.sub(r'\n[ \t\u00A0]+\n', '\n\n', text)
    paragraphs = re.split(r'\n\n+', text.strip())
    to_remove: set[int] = set()

    for i, para in enumerate(paragraphs):
        # From: / 差出人: 行を含む段落（メール境界のヘッダー）は署名扱いしない
        if _FROM_LINE_RE.search(para):
            continue

        # 結び言葉から始まる独立した短いブロックは署名として削除
        if _looks_like_signature_block(para):
            to_remove.add(i)
            # 後続の「短いブロック」も署名の続き（会社名/役職など）として除去
            k = i + 1
            while k < len(paragraphs):
                nxt = paragraphs[k]
                if not nxt.strip():
                    to_remove.add(k)
                    k += 1
                    continue
                if _FROM_LINE_RE.search(nxt):
                    break
                if _is_short_block(nxt):
                    to_remove.add(k)
                    k += 1
                    continue
                break
            continue

        if _SIG_INDICATOR_RE.search(para):
            to_remove.add(i)
            j = i - 1
            while j >= 0:
                block = paragraphs[j]
                if not block.strip():           # 空・空白のみの段落はスキップして続行
                    to_remove.add(j)
                    j -= 1
                    continue
                if not _is_short_block(block):
                    break
                if _CLOSING_RE.search(block):   # 結び言葉も署名の一部として除去し停止
                    to_remove.add(j)
                    break
                to_remove.add(j)
                j -= 1

    result = []
    for idx, p in enumerate(paragraphs):
        if idx in to_remove:
            continue
        # 段落内に途中から結び言葉が出る場合も削除
        p = _strip_inline_signature(p)
        if p.strip():
            result.append(p)
    return '\n\n'.join(result).strip()


def _is_image_attachment(att) -> bool:
    name = (getattr(att, "longFilename", None)
            or getattr(att, "shortFilename", None)
            or "")
    return name.lower().endswith(_IMAGE_EXTS)


def _attachment_cid(att) -> str:
    """添付の Content-ID を取得。extract_msg のバージョン差異を吸収する。"""
    for attr in ("cid", "contentId", "content_id"):
        v = getattr(att, attr, None)
        if v:
            return str(v).strip().strip("<>")
    return ""


def _clean_ocr_text(text: str) -> str:
    """OCR 結果から記号のみのノイズ行、極端に短いノイズを除去し、空行を圧縮する。"""
    cleaned: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            cleaned.append("")
            continue
        # 記号と空白だけの行は捨てる ( "*/", "/", "]", "[", "*", ",", "..." 等)
        if re.fullmatch(r'[\W_]+', s):
            continue
        # 文字数3文字未満かつ英数字を含まない行は捨てる
        if len(s) < 3 and not re.search(r'[A-Za-z0-9\u3040-\u30ff\u4e00-\u9fff]', s):
            continue
        cleaned.append(line)
    out = "\n".join(cleaned)
    # OCR ブロック内では空行は許可しない（最大 1 個の空行は最終整形で確保される）
    out = re.sub(r'\n\s*\n+', '\n', out)
    return out.strip()


def _ocr_image_bytes(data: bytes) -> str:
    """画像バイト列から OCR でテキストを抽出。失敗時は空文字。"""
    if not _OCR_AVAILABLE or pytesseract is None:
        return ""
    try:
        img = Image.open(io.BytesIO(data))
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
        raw = pytesseract.image_to_string(img, lang=_OCR_LANG)
        return _clean_ocr_text(raw)
    except Exception:
        return ""


def _format_ocr_block(name: str, ocr_text: str) -> str:
    """OCR結果を本文に埋め込む整形済みブロックに変換。"""
    label = name or "画像"
    if not _OCR_AVAILABLE:
        return f"[画像 {label}: OCR エンジン未インストール]"
    if not ocr_text:
        return f"[画像 {label}: 文字を検出できませんでした]"
    return f"[画像 {label} の文字認識結果:\n{ocr_text}\n]"


def _build_image_blocks(msg) -> tuple[dict, list, set]:
    """添付画像をすべて OCR し、cid→ブロック の辞書と画像情報リストを作る。"""
    cid_map: dict[str, str] = {}
    orphans: list[tuple[str, str, str]] = []
    matched_cids: set[str] = set()

    for att in getattr(msg, "attachments", []) or []:
        if not _is_image_attachment(att):
            continue
        data = getattr(att, "data", None)
        if not data:
            continue
        name = (getattr(att, "longFilename", None)
                or getattr(att, "shortFilename", None)
                or "image")
        ocr_text = _ocr_image_bytes(data)
        block = _format_ocr_block(name, ocr_text)
        cid = _attachment_cid(att)
        if cid:
            cid_map[cid] = block
        # ファイル名でも引けるようにする
        cid_map.setdefault(name, block)
        orphans.append((cid, name, block))

    # cid_map とは別に、HTML処理後に未使用だった画像を末尾に追記するためのリストを返す
    return cid_map, orphans, matched_cids


_BLOCK_TAGS_SINGLE_NL = ("p", "div", "br", "tr", "li", "blockquote", "pre")
_BLOCK_TAGS_DOUBLE_NL = ("h1", "h2", "h3", "h4", "h5", "h6")


def _normalize_html_to_text(html: str, cid_map: dict, matched_cids: set) -> str:
    """HTML 本文を整形してプレーンテキスト化する。
    - <a href="mailto:X"> を X テキストに置換（前後の &lt; &gt; と合わさり <X> になる）
    - <img> 位置に OCR テキストを埋め込む
    - 改行はブロック要素境界でのみ発生（インライン要素は連結）→ <a> 境界で行が割れない
    """
    soup = BeautifulSoup(html, "html.parser")

    # 画像位置に OCR テキストを埋め込む
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        replacement = None
        if src.lower().startswith("cid:"):
            cid_raw = src[4:]
            cid_clean = cid_raw.split("@", 1)[0]
            for key in (cid_raw, cid_clean):
                if key in cid_map:
                    replacement = cid_map[key]
                    matched_cids.add(key)
                    matched_cids.add(cid_clean)
                    break
        if replacement is None:
            for key, val in cid_map.items():
                if key and key in src and not src.startswith("data:"):
                    replacement = val
                    matched_cids.add(key)
                    break
        if replacement is None:
            replacement = "[画像]"
        img.replace_with("\n" + replacement + "\n")

    # mailto リンクをメアド文字列に置換（リンクテキスト境界で改行が入らないようにする）
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if href.lower().startswith("mailto:"):
            email = href[7:].split("?", 1)[0]
            a.replace_with(email)
        else:
            a.replace_with(a.get_text())

    # ブロック要素の末尾に明示的に改行を挿入してから separator='' で取得
    for tag in soup.find_all(list(_BLOCK_TAGS_SINGLE_NL)):
        tag.append("\n")
    for tag in soup.find_all(list(_BLOCK_TAGS_DOUBLE_NL)):
        tag.append("\n\n")

    text = soup.get_text(separator="")

    # &lt;<a>email</a>&gt; → <<email>> となるので <email> に正規化
    text = re.sub(
        r'<<\s*([\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+)\s*>>',
        r'<\1>',
        text,
    )
    return text


def extract_body(msg_path: str, do_ocr: bool = True) -> str:
    for enc in ("cp932", "utf-8", "latin-1"):
        try:
            msg = extract_msg.Message(msg_path, overrideEncoding=enc)
            try:
                if do_ocr:
                    cid_map, orphan_list, matched_cids = _build_image_blocks(msg)
                else:
                    cid_map, orphan_list, matched_cids = {}, [], set()
                has_images = bool(orphan_list)

                body = msg.body
                html = msg.htmlBody
                if isinstance(html, bytes):
                    try:
                        html = html.decode(enc, errors="replace")
                    except Exception:
                        html = html.decode("utf-8", errors="replace")

                # HTML 本文があればそれを優先（mailto 等のリンク構造を活かして整形できる）
                if html:
                    text = _normalize_html_to_text(html, cid_map, matched_cids)
                elif body and body.strip():
                    text = body
                else:
                    text = ""

                # HTML <img> 位置に埋め込めなかった画像（プレーン本文 or cid 解決失敗）を末尾追記
                if has_images:
                    used_blocks = set()
                    if has_images and html and text:
                        # text 内に既に挿入済みのブロックを判定
                        used_blocks = {b for b in (blk for _, _, blk in orphan_list) if b in text}
                    remaining = [blk for _, _, blk in orphan_list if blk not in used_blocks]
                    if remaining:
                        text = (text.rstrip() + "\n\n" + "\n\n".join(remaining))

                text = text.replace("\r\n", "\n").replace("\r", "\n")
                # 空白だけの行（NBSP含む）を空行に正規化
                text = re.sub(r'^[ \t\u00A0]+$', '', text, flags=re.MULTILINE)
                text = strip_signatures(strip_reply_headers(text))
                # 最終正規化：連続空行は最大1行 (= "\n\n") に
                text = re.sub(r'\n{3,}', '\n\n', text)
                return text.strip()
            finally:
                msg.close()
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return ""


# ── Streamlit UI ─────────────────────────────────────────────────────────────

st.set_page_config(page_title="メール整形ツール", page_icon="✉️", layout="wide")
st.title("✉️ メール整形ツール")
st.caption("返信ヘッダーと署名を除去してクリーンな本文を取り出します")

tab2, tab1 = st.tabs(["📋 テキスト貼り付け", "📁 ファイル一括処理（.msg）"])

# ── タブ1: .msg ファイルアップロード ──────────────────────────────────────────
with tab1:
    st.markdown("Outlookの `.msg` ファイルを複数アップロードして、整形済みテキストをZIPで受け取ります。")
    uploaded = st.file_uploader(
        ".msg ファイルを選択（複数可）",
        type=["msg"],
        accept_multiple_files=True,
    )

    ocr_default_help = (
        "添付画像（PNG/JPEG等）を Tesseract で文字認識し、本文中の画像位置に挿入します。"
        " 画像が多いと処理に時間がかかります。"
    )
    if not _OCR_AVAILABLE:
        st.warning(
            "⚠ OCR エンジン (Tesseract / pytesseract) が見つかりません。"
            "Windows ローカル実行の場合は `pytesseract` を `pip install` し、"
            "Tesseract 本体 (https://github.com/UB-Mannheim/tesseract/wiki) を導入してください。"
        )
    do_ocr = st.checkbox(
        "🔍 添付画像をOCRしてテキスト化する",
        value=_OCR_AVAILABLE,
        disabled=not _OCR_AVAILABLE,
        help=ocr_default_help,
    )

    if st.button("整形実行", key="btn_file", type="primary") and uploaded:
        zip_buf = io.BytesIO()
        log_placeholder = st.empty()
        logs: list[str] = []
        total = len(uploaded)
        progress = st.progress(0.0, text="準備中…")

        with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for idx, f in enumerate(uploaded, start=1):
                progress.progress(
                    (idx - 1) / total,
                    text=f"処理中: {f.name} ({idx}/{total})"
                    + ("  ※OCR有効" if do_ocr else ""),
                )
                with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as tmp:
                    tmp.write(f.read())
                    tmp_path = tmp.name
                try:
                    body = extract_body(tmp_path, do_ocr=do_ocr)
                    out_name = os.path.splitext(f.name)[0] + ".txt"
                    zf.writestr(out_name, body)
                    logs.append(f"✅ {f.name}")
                except Exception as e:
                    logs.append(f"❌ {f.name} — {e}")
                finally:
                    os.unlink(tmp_path)
                log_placeholder.text("\n".join(logs))
                progress.progress(idx / total, text=f"完了 {idx}/{total}")

        progress.empty()
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
    st.markdown(
        "任意のメール本文を貼り付けると、返信ヘッダーと署名を除去して返します。"
        "（入力後にフォーカスを外す/Tabキー/Ctrl+Enter で自動整形されます）"
    )

    col1, col_mid, col2 = st.columns([20, 1, 20])

    with col1:
        st.subheader("入力")
        st.text_area(
            "ここに貼り付け",
            height=520,
            placeholder="メール本文をここに貼り付けてください…",
            label_visibility="collapsed",
            key="raw_input",
        )

    # 入力欄の値が変わるたびに自動で整形する。
    # 整形後 widget は key を使わず value 引数で毎回描画する（key 経由 session_state では
    # 既存 widget に新値が反映されない Streamlit バージョンがあるため）。
    current_raw = st.session_state.get("raw_input", "") or ""
    formatted_text = (
        strip_signatures(strip_reply_headers(current_raw))
        if current_raw.strip()
        else ""
    )

    with col_mid:
        # 矢印は装飾用（自動整形なのでボタンではない）
        st.markdown(
            "<div style='margin-top:4.2rem; text-align:center; "
            "font-size:1.8rem; color:#888;'>→</div>",
            unsafe_allow_html=True,
        )

    with col2:
        st.subheader("整形後")
        st.text_area(
            "整形結果",
            value=formatted_text,
            height=520,
            label_visibility="collapsed",
        )

    # ── 入力と整形後のテキストエリアに対する UI 拡張 ──────────────────────────
    # 1) スクロール同期: ホイール／スクロールバー／キーボード操作すべてで
    #    左右が同時にスクロールするよう、親ドキュメントの textarea を aria-label で
    #    特定して相互に scrollTop を共有する。
    # 2) DeepL 風のクリア (×) ボタン: 値があるときだけ右上に表示し、
    #    クリック時に React (Streamlit) が検知できる方法で textarea を空にする。
    components.html(
        """
        <script>
        (function () {
            const parentWin = window.parent;
            const parentDoc = parentWin.document;
            const LEFT_LABEL = "ここに貼り付け";
            const RIGHT_LABEL = "整形結果";

            // React の制御コンポーネントに値変更を伝えるため、ネイティブ value setter 経由で
            // 書き換えてから input イベントを発火する（単に value="" としても無視される）。
            const nativeValueSetter = Object.getOwnPropertyDescriptor(
                parentWin.HTMLTextAreaElement.prototype, "value"
            ).set;
            function clearTextarea(ta) {
                nativeValueSetter.call(ta, "");
                ta.dispatchEvent(new Event("input", { bubbles: true }));
            }

            function ensureClearButton(ta) {
                // textarea のすぐ外側のラッパーを複数のセレクタで頑張って探す
                // (Streamlit のバージョンで data-baseweb / data-testid / クラス名が変わるため)
                const wrapper =
                    ta.closest('[data-baseweb="textarea"]') ||
                    ta.closest('[data-baseweb="base-input"]') ||
                    ta.closest('[data-testid="stTextArea"]') ||
                    ta.closest('.stTextArea') ||
                    ta.parentElement;
                if (!wrapper) return;
                if (wrapper.dataset.clearBtnInstalled === "1") {
                    const existing = wrapper.querySelector(".__clear_btn__");
                    if (existing) existing.style.display = ta.value.length ? "flex" : "none";
                    return;
                }
                wrapper.dataset.clearBtnInstalled = "1";
                if (getComputedStyle(wrapper).position === "static") {
                    wrapper.style.position = "relative";
                }

                const btn = parentDoc.createElement("button");
                btn.type = "button";
                btn.textContent = "×";
                btn.className = "__clear_btn__";
                btn.setAttribute("aria-label", "クリア");
                btn.setAttribute("title", "クリア");
                btn.style.cssText = [
                    "position:absolute",
                    "top:8px",
                    "right:10px",
                    "z-index:1000",
                    "width:24px",
                    "height:24px",
                    "padding:0",
                    "border:none",
                    "border-radius:50%",
                    "background:rgba(0,0,0,0.12)",
                    "color:#333",
                    "font-size:16px",
                    "font-weight:bold",
                    "line-height:1",
                    "cursor:pointer",
                    "display:none",
                    "align-items:center",
                    "justify-content:center",
                    "font-family:Arial, sans-serif",
                    "box-shadow:0 1px 2px rgba(0,0,0,0.15)",
                ].join(";");
                btn.addEventListener("mouseenter", function () {
                    btn.style.background = "rgba(0,0,0,0.20)";
                });
                btn.addEventListener("mouseleave", function () {
                    btn.style.background = "rgba(0,0,0,0.10)";
                });
                btn.addEventListener("mousedown", function (e) {
                    // textarea からフォーカスを奪わない
                    e.preventDefault();
                });
                btn.addEventListener("click", function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    clearTextarea(ta);
                    // 整形結果欄の × は、入力欄も連動してクリアする
                    // （自動整形なので入力欄が残っているとすぐに再表示されてしまうため）
                    if (ta.getAttribute("aria-label") === RIGHT_LABEL) {
                        const left = parentDoc.querySelector(
                            'textarea[aria-label="' + LEFT_LABEL + '"]'
                        );
                        if (left) clearTextarea(left);
                    }
                    btn.style.display = "none";
                });
                wrapper.appendChild(btn);

                function update() {
                    btn.style.display = ta.value.length > 0 ? "flex" : "none";
                }
                ta.addEventListener("input", update);
                // 値が外部から差し替わるケース（整形ボタン押下後の再描画など）にも追従
                const valueObserver = new MutationObserver(update);
                valueObserver.observe(ta, { attributes: true, attributeFilter: ["value"] });
                update();
            }

            function setupAutoClearOnEmpty(left, right) {
                // 入力欄が空になったら整形結果欄も即時クリアする（Streamlit の rerun を待たない）
                if (left.dataset.autoClearLinked === "1") return;
                left.dataset.autoClearLinked = "1";
                left.addEventListener("input", function () {
                    if (left.value.length === 0 && right.value.length > 0) {
                        clearTextarea(right);
                    }
                });
            }

            function setupScrollSync(left, right) {
                if (left.dataset.scrollSync === "1" && right.dataset.scrollSync === "1") {
                    return;
                }
                left.dataset.scrollSync = "1";
                right.dataset.scrollSync = "1";

                let syncing = false;
                function makeHandler(src, dst) {
                    return function () {
                        if (syncing) return;
                        syncing = true;
                        const maxSrc = src.scrollHeight - src.clientHeight;
                        const maxDst = dst.scrollHeight - dst.clientHeight;
                        if (maxSrc > 0 && maxDst > 0) {
                            // 行数が違っても割合で揃える
                            dst.scrollTop = (src.scrollTop / maxSrc) * maxDst;
                        } else {
                            dst.scrollTop = src.scrollTop;
                        }
                        parentWin.requestAnimationFrame(function () {
                            syncing = false;
                        });
                    };
                }
                left.addEventListener("scroll", makeHandler(left, right));
                right.addEventListener("scroll", makeHandler(right, left));
            }

            function attach() {
                const left = parentDoc.querySelector(
                    'textarea[aria-label="' + LEFT_LABEL + '"]'
                );
                const right = parentDoc.querySelector(
                    'textarea[aria-label="' + RIGHT_LABEL + '"]'
                );
                if (!left || !right) return false;

                ensureClearButton(left);
                ensureClearButton(right);
                setupScrollSync(left, right);
                setupAutoClearOnEmpty(left, right);
                return true;
            }

            // 初回アタッチ（Streamlit の再描画でまだ要素が無いことがある）
            if (!attach()) {
                const interval = setInterval(function () {
                    if (attach()) clearInterval(interval);
                }, 150);
                setTimeout(function () { clearInterval(interval); }, 10000);
            }

            // 再描画で textarea が差し替わった場合に再アタッチする
            const observer = new MutationObserver(function () { attach(); });
            observer.observe(parentDoc.body, { childList: true, subtree: true });
        })();
        </script>
        """,
        # height=0 だと iframe が完全に隠され、内部 JS が実行されない Streamlit バージョンが
        # あるため、最小限のサイズを確保する
        height=1,
    )
