import io
import os
import re
import tempfile
import zipfile

import extract_msg
import streamlit as st
import streamlit.components.v1 as components
from bs4 import BeautifulSoup

# ── テキスト処理ロジック ─────────────────────────────────────────────────────

_SEPARATOR_RE = re.compile(r'^[ \t]*[-_=*]{5,}[^\n]*$\r?\n?', re.MULTILINE)
_REPLY_HEADER_RE = re.compile(
    r'^[ \t]*(From|Sent|To|Cc|Bcc|Subject|Date|差出人|送信日時|宛先|件名|CC|BCC)\s*:.*$\r?\n?',
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


def _is_short_block(text: str) -> bool:
    lines = [l for l in text.splitlines() if l.strip()]
    return bool(lines) and len(lines) <= 5 and all(len(l.strip()) <= 55 for l in lines)


def strip_reply_headers(text: str) -> str:
    text = _SEPARATOR_RE.sub('', text)
    text = _REPLY_HEADER_RE.sub('', text)
    text = re.sub(r'^[ \t]*Internal[ \t]*$', '=' * 52, text, flags=re.MULTILINE | re.IGNORECASE)
    text = re.sub(r'^[ \t]+$', '', text, flags=re.MULTILINE)  # 空白のみの行を空行に
    text = re.sub(r'\n{3,}', '\n\n', text)                    # 連続する空行を1行に
    return text.strip()


def strip_signatures(text: str) -> str:
    # 空白文字だけの行（"\n \n" など）を空行に正規化してから分割
    text = re.sub(r'\n[ \t]+\n', '\n\n', text)
    paragraphs = re.split(r'\n\n+', text.strip())
    to_remove: set[int] = set()

    for i, para in enumerate(paragraphs):
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

    result = [p for idx, p in enumerate(paragraphs) if idx not in to_remove and p.strip()]
    return '\n\n'.join(result).strip()


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

    with col_mid:
        # テキストエリアのラベル分（subheader + margin）を合わせて縦中央に寄せる
        st.markdown("<div style='margin-top:3.6rem'></div>", unsafe_allow_html=True)
        if st.button("→", key="btn_paste", type="primary", use_container_width=True):
            current_raw = st.session_state.get("raw_input", "")
            if current_raw.strip():
                # 整形後 textarea の key と同じ名前で書き込めば、次の描画でそのまま反映される
                st.session_state["paste_result"] = strip_signatures(
                    strip_reply_headers(current_raw)
                )

    with col2:
        st.subheader("整形後")
        st.text_area(
            "整形結果",
            height=520,
            label_visibility="collapsed",
            key="paste_result",
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
                // textarea のすぐ外側のラッパー (data-baseweb="textarea") を基準に絶対配置
                const wrapper =
                    ta.closest('[data-baseweb="textarea"]') || ta.parentElement;
                if (!wrapper) return;
                if (wrapper.dataset.clearBtnInstalled === "1") {
                    // 既に設置済みでも表示状態だけは現在値に合わせる
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
                    "top:6px",
                    "right:8px",
                    "z-index:50",
                    "width:22px",
                    "height:22px",
                    "padding:0",
                    "border:none",
                    "border-radius:50%",
                    "background:rgba(0,0,0,0.10)",
                    "color:#333",
                    "font-size:15px",
                    "line-height:1",
                    "cursor:pointer",
                    "display:none",
                    "align-items:center",
                    "justify-content:center",
                    "font-family:Arial, sans-serif",
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
        height=0,
    )
