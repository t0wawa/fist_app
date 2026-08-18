import os
import re
import streamlit as st
import fitz  # PyMuPDF
import pandas as pd
from PIL import Image
from google import genai
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
import tempfile
from html2image import Html2Image

# 日本語フォントの設定（PDF出力用）
pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))

# --- 自動モデル切り替え機能 ---
def generate_with_auto_fallback(client, contents):
    if "ai_model" not in st.session_state:
        st.session_state.ai_model = 'gemini-2.5-flash'
        
    try:
        return client.models.generate_content(
            model=st.session_state.ai_model,
            contents=contents
        )
    except Exception as e:
        error_msg = str(e)
        match = re.search(r'use models/(gemini-[\w\.-]+)', error_msg)
        if match:
            new_model = match.group(1)
            st.session_state.ai_model = new_model
            return client.models.generate_content(
                model=st.session_state.ai_model,
                contents=contents
            )
        else:
            raise e

# --- レイアウトを再構築して新しい画像を生成する機能 ---
def recreate_clean_image(client, img_path, temp_dir, page_num):
    pil_img = Image.open(img_path)
    prompt = """
    添付された画像から、手書きの書き込みやメモをすべて無視し、印刷されている活字の問題文と図表のみを抽出してください。
    そして、元の画像のレイアウト（配置や段組み）を忠実に再現した、1ページの完全なHTMLコードを作成してください。
    - 図形やグラフがある場合は、SVGコードを用いてHTML内に直接描画してください。
    - 出力はHTMLコードのみとし、```html と ``` で囲んで出力してください。
    """
    
    response = generate_with_auto_fallback(client, [pil_img, prompt])
    
    # HTMLコード部分だけを抽出
    html_match = re.search(r'```html\n(.*?)\n```', response.text, re.DOTALL)
    html_content = html_match.group(1) if html_match else response.text

    # HTMLを新しい画像としてレンダリング（A4サイズ比率）
    hti = Html2Image(output_path=temp_dir)
    out_filename = f"clean_page_{page_num}.png"
    hti.screenshot(html_str=html_content, save_as=out_filename, size=(794, 1123))
    
    return os.path.join(temp_dir, out_filename)

# --- UI設定 ---
st.set_page_config(page_title="過去問クリーニング＆解答生成アプリ", layout="wide")
st.title("📄 過去問クリーニング & 解答解説自動生成ツール")

# 状態保持
if "processed_data" not in st.session_state:
    st.session_state.processed_data = []
if "clean_image_paths" not in st.session_state:
    st.session_state.clean_image_paths = []
if "pdf_base_name" not in st.session_state:
    st.session_state.pdf_base_name = "output"
# PDF生成状態の保持（ダウンロード時の再実行防止）
if "pdf_created" not in st.session_state:
    st.session_state.pdf_created = False

# --- サイドバー (設定) ---
with st.sidebar:
    st.header("⚙️ 設定")
    api_key = st.text_input("Gemini APIキーを入力", type="password")
    if "ai_model" in st.session_state:
        st.write(f"🤖 現在使用中のAI: `{st.session_state.ai_model}`")

# --- メイン画面 ---
uploaded_file = st.file_uploader("先輩の過去問PDFをアップロードしてください", type=["pdf"])

if uploaded_file and api_key:
    st.session_state.pdf_base_name = os.path.splitext(uploaded_file.name)[0]
    
    if st.button("🚀 1. スキャン＆画像再生成 ＆ 解答作成スタート"):
        st.session_state.processed_data = []
        st.session_state.pdf_created = False # リセット
        client = genai.Client(api_key=api_key)
        
        with st.spinner("レイアウトを解析し、新しい画像を再生成中...（少し時間がかかります）"):
            temp_dir = tempfile.mkdtemp()
            pdf_path = os.path.join(temp_dir, "temp.pdf")
            with open(pdf_path, "wb") as f:
                f.write(uploaded_file.read())
            
            # PDF → 画像化
            doc = fitz.open(pdf_path)
            raw_image_paths = []
            for i, page in enumerate(doc):
                pix = page.get_pixmap(dpi=200)
                img_path = os.path.join(temp_dir, f"raw_page_{i+1}.png")
                pix.save(img_path)
                raw_image_paths.append(img_path)
            
            # 手書きを除去した新しいレイアウト画像の生成
            st.session_state.clean_image_paths = []
            for i, raw_img in enumerate(raw_image_paths):
                clean_img = recreate_clean_image(client, raw_img, temp_dir, i+1)
                st.session_state.clean_image_paths.append(clean_img)
            
            # 解答解説の生成（LaTeX記号の禁止を徹底）
            prompt = """
            画像内のすべての問題について解き、以下のフォーマットで出力してください。問題ごとに「---」で区切ってください。
            【重要ルール】
            数式や記号に $ や $$ などのLaTeX記号は「絶対に」使用しないでください。通常のテキストと算術記号（例: f(x), x^2, ×, ÷）のみで出力してください。

            問題番号: [番号]
            問題文: [テキスト]
            解答: [模範解答]
            解説: [解説]
            """
            
            for i, img_path in enumerate(st.session_state.clean_image_paths):
                pil_img = Image.open(img_path)
                response = generate_with_auto_fallback(client, [pil_img, prompt])
                
                blocks = response.text.split("---")
                for j, block in enumerate(blocks):
                    if not block.strip(): continue
                    lines = [line.strip() for line in block.strip().split("\n") if line.strip()]
                    q_num, q_text, ans, exp = f"P{i+1}_{j+1}", "", "", ""
                    for line in lines:
                        if line.startswith("問題番号:"): q_num = line.replace("問題番号:", "").strip()
                        elif line.startswith("問題文:"): q_text = line.replace("問題文:", "").strip()
                        elif line.startswith("解答:"): ans = line.replace("解答:", "").strip()
                        elif line.startswith("解説:"): exp = line.replace("解説:", "").strip()
                    
                    if q_text or ans:
                        st.session_state.processed_data.append({
                            "id": q_num, "question_text": q_text, "ai_answer": ans, "ai_explanation": exp,
                            "human_answer": "", "final_answer": ans, "final_explanation": exp
                        })
        st.success("再生成が完了しました！下で結果を確認・修正してください。")

# --- 結果の確認・修正フロー ---
if st.session_state.processed_data:
    st.divider()
    st.subheader("🧐 2. 結果の確認と修正")
    
    with st.expander("AIがスキャンして再配置した新しい画像を見る"):
        cols = st.columns(len(st.session_state.clean_image_paths))
        for idx, img_p in enumerate(st.session_state.clean_image_paths):
            cols[idx].image(img_p, caption=f"ページ {idx+1}", use_container_width=True)

    df = pd.DataFrame(st.session_state.processed_data)
    edited_df = st.data_editor(
        df[["id", "question_text", "ai_answer", "ai_explanation", "human_answer"]], 
        use_container_width=True, num_rows="dynamic"
    )

    if st.button("✅ 3. 修正を反映して最終PDFを作成する"):
        with st.spinner("最終PDFを生成中..."):
            client = genai.Client(api_key=api_key)
            final_results = []
            
            for idx, row in edited_df.iterrows():
                final_ans = row["ai_answer"]
                final_exp = row["ai_explanation"]
                
                if pd.notna(row["human_answer"]) and str(row["human_answer"]).strip() != "":
                    final_ans = str(row["human_answer"]).strip()
                    re_prompt = f"問題: {row['question_text']}\n正しい解答: {final_ans}\nこの正しい解答に合わせたわかりやすい解説を再作成してください。LaTeX記号（$など）は絶対に使用しないでください。"
                    res = generate_with_auto_fallback(client, re_prompt)
                    final_exp = res.text.strip()
                
                final_results.append({
                    "id": row["id"], "final_answer": final_ans, "final_explanation": final_exp
                })

            temp_dir2 = tempfile.mkdtemp()
            clean_pdf_out = os.path.join(temp_dir2, f"{st.session_state.pdf_base_name}_問題のみ.pdf")
            doc_out = fitz.open()
            for img_path in st.session_state.clean_image_paths:
                img_doc = fitz.open(img_path)
                pdf_bytes = img_doc.convert_to_pdf()
                doc_out.insert_pdf(fitz.open("pdf", pdf_bytes))
            doc_out.save(clean_pdf_out)
            doc_out.close()

            ans_pdf_out = os.path.join(temp_dir2, "answers.pdf")
            doc_rl = SimpleDocTemplate(ans_pdf_out, pagesize=A4)
            style_normal = ParagraphStyle('JpNormal', fontName='HeiseiKakuGo-W5', fontSize=10, leading=14)
            style_h2 = ParagraphStyle('JpH2', fontName='HeiseiKakuGo-W5', fontSize=14, leading=18, spaceAfter=6)
            story = [Paragraph("解答・解説集", ParagraphStyle('Title', fontName='HeiseiKakuGo-W5', fontSize=18)), Spacer(1, 12)]
            
            for res in final_results:
                story.append(Paragraph(f"【問題】 {res['id']}", style_h2))
                story.append(Paragraph(f"正解: {res['final_answer']}", style_normal))
                story.append(Paragraph(f"解説: {res['final_explanation']}", style_normal))
                story.append(Spacer(1, 10))
            doc_rl.build(story)

            combined_pdf_out = os.path.join(temp_dir2, f"{st.session_state.pdf_base_name}_問題+解答解説.pdf")
            final_doc = fitz.open(clean_pdf_out)
            final_doc.insert_pdf(fitz.open(ans_pdf_out))
            final_doc.save(combined_pdf_out)
            final_doc.close()

            # 生成したファイルのパスを保存（ボタン再実行防止）
            st.session_state.pdf_created = True
            st.session_state.clean_pdf_out = clean_pdf_out
            st.session_state.combined_pdf_out = combined_pdf_out
            
            st.success("🎉 PDFの作成が完了しました！")

    # 一度作成されたら、ボタンが押されなくてもダウンロードボタンを表示し続ける
    if st.session_state.pdf_created:
        col1, col2 = st.columns(2)
        with open(st.session_state.clean_pdf_out, "rb") as f1:
            col1.download_button("📥 「問題のみ.pdf」をダウンロード", f1, file_name=f"{st.session_state.pdf_base_name}_問題のみ.pdf", mime="application/pdf")
        with open(st.session_state.combined_pdf_out, "rb") as f2:
            col2.download_button("📥 「問題+解答解説.pdf」をダウンロード", f2, file_name=f"{st.session_state.pdf_base_name}_問題+解答解説.pdf", mime="application/pdf")

elif not api_key:
    st.warning("👈 左のサイドバーにAPIキーを入力してください。")