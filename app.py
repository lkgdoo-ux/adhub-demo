# app.py — AdHub v3
import streamlit as st
import pandas as pd
import psycopg2, json
from datetime import datetime
import plotly.express as px
import plotly.graph_objects as go

st.set_page_config(page_title="AdHub", page_icon="📊", layout="wide")

DB_URL = st.secrets["DB_URL"]

# ============ DB ============
def init_db():
    conn=psycopg2.connect(DB_URL); cur=conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY,name TEXT,role TEXT,password TEXT);")
    cur.execute("CREATE TABLE IF NOT EXISTS advertisers (code TEXT PRIMARY KEY,name TEXT,total_budget FLOAT DEFAULT 0,created_at TIMESTAMP DEFAULT NOW());")
    cur.execute("CREATE TABLE IF NOT EXISTS permissions (email TEXT,advertiser_code TEXT,level TEXT,PRIMARY KEY (email,advertiser_code));")
    cur.execute("CREATE TABLE IF NOT EXISTS perf (id SERIAL PRIMARY KEY,advertiser_code TEXT,platform TEXT,date TEXT,campaign TEXT,adgroup TEXT,impressions INT,clicks INT,cost FLOAT,raw_data TEXT);")
    cur.execute("CREATE TABLE IF NOT EXISTS upload_log (id SERIAL PRIMARY KEY,email TEXT,advertiser_code TEXT,platform TEXT,file_name TEXT,rows INT,uploaded_at TIMESTAMP DEFAULT NOW());")
    cur.execute("CREATE TABLE IF NOT EXISTS conversion_mapping (id SERIAL PRIMARY KEY,advertiser_code TEXT,platform TEXT,campaign TEXT,conversion_column TEXT,conversion_label TEXT,updated_at TIMESTAMP DEFAULT NOW(),UNIQUE(advertiser_code,platform,campaign));")
    conn.commit(); cur.close(); conn.close()

def migrate_db(): pass

init_db(); migrate_db()

def q(sql,params=(),fetch=True):
    conn=psycopg2.connect(DB_URL); cur=conn.cursor(); cur.execute(sql,params)
    rows=cur.fetchall() if fetch else None
    conn.commit(); cur.close(); conn.close(); return rows

def safe_div(a,b): return (a/b) if b else 0

import secrets

def create_viewer_account(adv_code,adv_name):
    email=f"viewer_{adv_code.lower()}@adhub.com"; temp_pw=secrets.token_urlsafe(6)
    q("INSERT INTO users (email,name,role,password) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",(email,f"{adv_name}_뷰어","VIEWER",temp_pw),fetch=False)
    q("INSERT INTO permissions (email,advertiser_code,level) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",(email,adv_code,"VIEWER"),fetch=False)
    return email,temp_pw
# ============ 로그인 ============
def login_view():
    st.title("📊 AdHub 로그인")
    st.caption("데모 계정: admin@adhub.com / manager@scon.com / viewer@scon.com (비번: 1234)")
    email = st.text_input("이메일"); pw = st.text_input("비밀번호", type="password")
    if st.button("로그인", type="primary"):
        row = q("SELECT email,name,role FROM users WHERE email=%s AND password=%s", (email, pw))
        if row:
            r = row[0]
            st.session_state.user = {"email": r[0], "name": r[1], "role": r[2]}; st.rerun()
        else: st.error("로그인 실패")

if "user" not in st.session_state:
    login_view(); st.stop()
user = st.session_state.user
is_admin = user["role"] in ("AGENCY_ADMIN", "SUPER_ADMIN")

# ============ 사이드바 ============
my_advs = q("""SELECT a.code, a.name, p.level FROM permissions p
    JOIN advertisers a ON a.code=p.advertiser_code WHERE p.email=%s ORDER BY a.name""", (user["email"],))

with st.sidebar:
    st.markdown(f"**👤 {user['name']}**  \n`{user['role']}`")
    if st.button("로그아웃"): del st.session_state.user; st.rerun()
    st.divider()
    if not my_advs:
        st.warning("접근 가능한 광고주 없음"); adv_code, my_level, sel_name = None, None, None
    else:
        adv_options = {f"{name} ({code})": (code, level) for code, name, level in my_advs}
        sel = st.selectbox("광고주 선택", list(adv_options.keys()))
        adv_code, my_level = adv_options[sel]; sel_name = sel
        st.info(f"권한: **{my_level}**")
    menu = ["📈 대시보드", "📥 PDF 리포트"]
    if my_level in ("OWNER","EDITOR") or is_admin:
        menu += ["📤 데이터 업로드", "📋 업로드 이력", "🎯 전환지표 설정"]
    if is_admin: menu.append("🏢 광고주 관리")
    if is_admin: menu.append("👤 계정 관리")
    page = st.radio("메뉴", menu)

# ============ 컬럼 매퍼 ============
GOOGLE_CORE = {"일":"date","캠페인":"campaign","광고그룹":"adgroup","노출수":"impressions","클릭수":"clicks","비용":"cost"}
GOOGLE_CONV_CANDIDATES = ["전환수","설치","사전예약"]
FB_CORE = {"보고 시작":"date","캠페인 이름":"campaign","광고 세트 이름":"adgroup","노출":"impressions","클릭(전체)":"clicks","지출 금액 (KRW)":"cost","지출 금액 (USD)":"cost_usd"}
FB_CONV_CANDIDATES = ["결과","페이지 참여","링크 클릭","게시물 공감","게시물 댓글","팔로우 또는 좋아요"]

# ============ 컬럼 매핑 헬퍼 ============
DATE_CANDS = ["일", "날짜", "date", "보고 시작", "보고 시작일", "Day"]
CAMP_CANDS = ["캠페인", "캠페인 이름", "campaign", "campaign name"]
AG_CANDS   = ["광고그룹", "광고 세트 이름", "광고세트 이름", "광고세트", "adgroup", "adset", "ad set name"]
IMP_CANDS  = ["노출수", "노출", "impressions", "impression"]
CLK_CANDS  = ["클릭수", "클릭(전체)", "링크 클릭", "고유 링크 클릭", "클릭", "clicks", "link clicks"]
COST_CANDS = ["비용", "지출 금액 (KRW)", "지출 금액 (USD)", "지출 금액", "cost", "spend", "amount spent"]
CREATIVE_CANDS = ["소재", "광고소재", "소재명", "광고 이름", "광고 이름(광고)", "ad name", "creative", "creative name", "ad", "광고"]

def guess_column(columns, candidates):
    """후보 리스트에서 첫 번째로 일치하는 컬럼 반환 (정확 일치 → 부분 일치 순)"""
    cols_lower = {c.lower(): c for c in columns}
    # 정확 일치
    for cand in candidates:
        if cand.lower() in cols_lower:
            return cols_lower[cand.lower()]
    # 부분 일치
    for cand in candidates:
        for col in columns:
            if cand.lower() in col.lower():
                return col
    return None

def read_uploaded_file(file):
    """파일을 그대로 DataFrame으로 읽기 (매핑 전)"""
    name = file.name.lower()
    if name.endswith(("xlsx", "xls")):
        df = pd.read_excel(file)
    else:
        df = pd.read_csv(file)
    df.columns = [str(c).strip() for c in df.columns]
    return df

def parse_file(file, platform):
    df = pd.read_excel(file) if file.name.lower().endswith(("xlsx","xls")) else pd.read_csv(file)
    df.columns = [str(c).strip() for c in df.columns]
    if platform == "GOOGLE":
        core_map, conv_cands = GOOGLE_CORE, GOOGLE_CONV_CANDIDATES
    else:
        core_map, conv_cands = FB_CORE, FB_CONV_CANDIDATES
    rename = {k:v for k,v in core_map.items() if k in df.columns}
    df = df.rename(columns=rename)
    if platform == "FACEBOOK" and "cost" not in df.columns and "cost_usd" in df.columns:
        df["cost"] = pd.to_numeric(df["cost_usd"], errors="coerce").fillna(0) * 1300
    if "adgroup" not in df.columns: df["adgroup"] = df.get("campaign", "")
    for c in ["date","campaign","adgroup","impressions","clicks","cost"]:
        if c not in df.columns: df[c] = 0
    conv_present = [c for c in conv_cands if c in df.columns]
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["date"])
    def make_raw(row):
        d = {}
        for c in conv_present:
            v = row.get(c)
            try: d[c] = float(v) if pd.notna(v) else 0
            except: d[c] = 0
        return json.dumps(d, ensure_ascii=False)
    df["raw_data"] = df.apply(make_raw, axis=1)
    return df[["date","campaign","adgroup","impressions","clicks","cost","raw_data"]], conv_present

# ============ 전환 매핑 ============
def get_conversion_mapping(adv_code):
    rows = q("SELECT platform, campaign, conversion_column, conversion_label FROM conversion_mapping WHERE advertiser_code=%s", (adv_code,))
    return {(p, c): (col, lbl) for p, c, col, lbl in rows}

def resolve_conv(mapping, platform, campaign):
    if (platform, campaign) in mapping: return mapping[(platform, campaign)]
    if (platform, "*") in mapping: return mapping[(platform, "*")]
    return (None, "CPA")

def compute_metrics(df, mapping):
    if df.empty:
        df["conversions"] = 0; df["conv_label"] = "CPA"; df["conv_column"] = ""; return df
    def get_conv(row):
        col, _ = resolve_conv(mapping, row["platform"], row["campaign"])
        if not col: return 0
        try:
            d = json.loads(row["raw_data"]) if row["raw_data"] else {}
            return float(d.get(col, 0))
        except: return 0
    def get_label(row):
        _, lbl = resolve_conv(mapping, row["platform"], row["campaign"]); return lbl
    def get_col(row):
        col, _ = resolve_conv(mapping, row["platform"], row["campaign"]); return col or ""
    df = df.copy()
    df["conversions"] = df.apply(get_conv, axis=1)
    df["conv_label"] = df.apply(get_label, axis=1)
    df["conv_column"] = df.apply(get_col, axis=1)
    return df
# ============ PDF 리포트 생성 ============
import io

def build_pdf_report(adv_code, adv_name, df_all, total_budget, show_conv):
    """간단한 PDF 리포트 (표지 + 예산 + KPI + 차트 + 캠페인 TOP 15)"""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    
    # 한글 폰트 등록 시도
    font_name = "Helvetica"
    import os
    candidate_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "NanumGothic.ttf"),
        "NanumGothic.ttf",
        "./NanumGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    for path in candidate_paths:
        try:
            if os.path.exists(path):
                pdfmetrics.registerFont(TTFont("Korean", path))
                font_name = "Korean"
                break
        except Exception:
            continue
    
    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Title"], fontName=font_name,
                                  fontSize=22, textColor=colors.HexColor("#1F2937"), spaceAfter=20)
    h2_style = ParagraphStyle("h2", parent=styles["Heading2"], fontName=font_name,
                               fontSize=14, textColor=colors.HexColor("#1F2937"),
                               spaceAfter=12, spaceBefore=12)
    body_style = ParagraphStyle("body", parent=styles["BodyText"], fontName=font_name, fontSize=10)
    
    story = []
    
    tot_imp = int(df_all["impressions"].sum())
    tot_clk = int(df_all["clicks"].sum())
    tot_cost = float(df_all["cost"].sum())
    tot_conv = float(df_all["conversions"].sum())
    burn = safe_div(tot_cost, total_budget) * 100 if total_budget else 0
    period = f"{df_all['date'].min().strftime('%Y-%m-%d')} ~ {df_all['date'].max().strftime('%Y-%m-%d')}"
    labels = sorted(set(df_all["conv_label"].dropna().unique()))
    conv_label = "/".join(labels) if labels else "CPA"
    
    # 표지
    story.append(Paragraph(f"📊 {adv_name}", title_style))
    story.append(Paragraph("광고 성과 리포트", h2_style))
    story.append(Spacer(1, 12))
    story.append(Paragraph(f"기간: {period}", body_style))
    story.append(Paragraph(f"매체: {', '.join(sorted(df_all['platform'].unique()))}", body_style))
    story.append(Paragraph(f"생성일: {datetime.now().strftime('%Y-%m-%d %H:%M')}", body_style))
    story.append(Spacer(1, 18))
    
    # 예산 요약
    if total_budget:
        story.append(Paragraph("예산 현황", h2_style))
        budget_data = [
            ["총 예산", f"₩{total_budget:,.0f}"],
            ["사용 예산", f"₩{tot_cost:,.0f}"],
            ["남은 예산", f"₩{max(total_budget - tot_cost, 0):,.0f}"],
            ["소진율", f"{burn:.1f}%"],
        ]
        t = Table(budget_data, colWidths=[5*cm, 6*cm])
        t.setStyle(TableStyle([
            ("FONTNAME", (0,0), (-1,-1), font_name),
            ("FONTSIZE", (0,0), (-1,-1), 10),
            ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#F3F4F6")),
            ("TEXTCOLOR", (1,3), (1,3), colors.HexColor("#ef4444")),
            ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#d1d5db")),
            ("PADDING", (0,0), (-1,-1), 6),
        ]))
        story.append(t)
        story.append(Spacer(1, 14))
    
    # KPI
    story.append(Paragraph("핵심 지표", h2_style))
    kpi_data = [["지표", "값"],
                ["노출 (Impression)", f"{tot_imp:,}"],
                ["클릭 (Click)", f"{tot_clk:,}"],
                ["광고비 (Cost)", f"₩{tot_cost:,.0f}"],
                ["CTR", f"{safe_div(tot_clk, tot_imp)*100:.2f}%"],
                ["CPM", f"₩{safe_div(tot_cost, tot_imp)*1000:,.0f}"],
                ["CPC", f"₩{safe_div(tot_cost, tot_clk):,.0f}"]]
    if show_conv:
        kpi_data.append([f"전환 ({conv_label})", f"{tot_conv:,.0f}"])
        kpi_data.append([conv_label, f"₩{safe_div(tot_cost, tot_conv):,.0f}" if tot_conv else "—"])
    t = Table(kpi_data, colWidths=[6*cm, 6*cm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-1), font_name),
        ("FONTSIZE", (0,0), (-1,-1), 10),
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#4285F4")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#d1d5db")),
        ("PADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(t)
    story.append(PageBreak())
    
    # 일자별 광고비 차트 (Plotly → PNG)
    try:
        story.append(Paragraph("일자별 광고비 추이", h2_style))
        daily = df_all.groupby(["date","platform"], as_index=False)["cost"].sum()
        fig = px.line(daily, x="date", y="cost", color="platform", markers=True,
                      color_discrete_map={"GOOGLE":"#4285F4","FACEBOOK":"#1877F2"})
        fig.update_layout(width=700, height=350, margin=dict(t=20, b=40, l=60, r=20))
        img_bytes = fig.to_image(format="png", scale=2)
        img = Image(io.BytesIO(img_bytes), width=16*cm, height=8*cm)
        story.append(img)
        story.append(Spacer(1, 10))
    except Exception as e:
        story.append(Paragraph(f"(차트 생성 생략: {str(e)[:60]})", body_style))
    
    # 캠페인별 성과 TOP 15
    story.append(Paragraph("캠페인별 성과 TOP 15", h2_style))
    by_camp = df_all.groupby(["platform","campaign"], as_index=False).agg(
        impressions=("impressions","sum"), clicks=("clicks","sum"),
        cost=("cost","sum"), conversions=("conversions","sum"))
    by_camp = by_camp.sort_values("cost", ascending=False).head(15)
    
    head = ["매체","캠페인","노출","클릭","광고비","CTR"]
    if show_conv: head.append(conv_label)
    table_data = [head]
    for _, row in by_camp.iterrows():
        camp = row["campaign"][:30] + ("…" if len(row["campaign"]) > 30 else "")
        line = [row["platform"], camp,
                f"{int(row['impressions']):,}", f"{int(row['clicks']):,}",
                f"₩{int(row['cost']):,}",
                f"{safe_div(row['clicks'], row['impressions'])*100:.2f}%"]
        if show_conv:
            line.append(f"₩{safe_div(row['cost'], row['conversions']):,.0f}" if row["conversions"] else "—")
        table_data.append(line)
    t = Table(table_data, repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-1), font_name),
        ("FONTSIZE", (0,0), (-1,-1), 8),
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#4285F4")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#d1d5db")),
        ("ALIGN", (2,1), (-1,-1), "RIGHT"),
        ("PADDING", (0,0), (-1,-1), 4),
    ]))
    story.append(t)
    
    doc.build(story)
    output.seek(0)
    return output.getvalue()

# ============ 차트/KPI 헬퍼 ============
def chart_daily_metric(df, conv_label, key_prefix=""):
    if df.empty: return
    metric_choice = st.radio(
        "지표 선택", ["CTR (%)","CPM (₩)","CPC (₩)",f"{conv_label} (₩)"],
        horizontal=True, key=f"{key_prefix}_metric")
    daily = df.groupby(["date","platform"], as_index=False).agg(
        impressions=("impressions","sum"), clicks=("clicks","sum"),
        cost=("cost","sum"), conversions=("conversions","sum"))
    daily["CTR"] = daily.apply(lambda r: safe_div(r.clicks, r.impressions)*100, axis=1)
    daily["CPM"] = daily.apply(lambda r: safe_div(r.cost, r.impressions)*1000, axis=1)
    daily["CPC"] = daily.apply(lambda r: safe_div(r.cost, r.clicks), axis=1)
    daily["CPA"] = daily.apply(lambda r: safe_div(r.cost, r.conversions), axis=1)
    mmap = {"CTR (%)":"CTR","CPM (₩)":"CPM","CPC (₩)":"CPC",f"{conv_label} (₩)":"CPA"}
    y_col = mmap[metric_choice]
    fig = px.line(daily, x="date", y=y_col, color="platform", markers=True,
                  title=f"일자별 {metric_choice} 추이",
                  color_discrete_map={"GOOGLE":"#4285F4","FACEBOOK":"#1877F2"})
    fig.update_layout(height=380, hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_daily_chart")

def chart_cost_donut(df, title="매체별 광고비 비중"):
    by_pf = df.groupby("platform", as_index=False)["cost"].sum()
    if by_pf.empty or by_pf["cost"].sum()==0:
        st.info("광고비 데이터 없음"); return
    fig = px.pie(by_pf, names="platform", values="cost", hole=0.5, title=title,
                 color="platform", color_discrete_map={"GOOGLE":"#4285F4","FACEBOOK":"#1877F2"})
    fig.update_traces(textposition='inside', textinfo='percent+label')
    fig.update_layout(height=350)
    st.plotly_chart(fig, use_container_width=True, key=f"cost_donut_{title}")

def chart_campaign_bar(df, metric="cost", title="캠페인별 광고비 TOP 10"):
    by_camp = df.groupby(["campaign","platform"], as_index=False).agg(
        cost=("cost","sum"), clicks=("clicks","sum"),
        impressions=("impressions","sum"), conversions=("conversions","sum"))
    by_camp = by_camp.sort_values(metric, ascending=False).head(10)
    if by_camp.empty: return
    fig = px.bar(by_camp, x=metric, y="campaign", color="platform", orientation="h",
                 title=title, color_discrete_map={"GOOGLE":"#4285F4","FACEBOOK":"#1877F2"})
    fig.update_layout(height=400, yaxis={"categoryorder":"total ascending"})
    st.plotly_chart(fig, use_container_width=True, key=f"camp_bar_{title}")

# ⭐ 신규: 예산 소진율 도넛
def render_budget_donut(spent, total, height=240):
    remaining = max(total - spent, 0)
    burn = safe_div(spent, total) * 100
    fig = go.Figure(data=[go.Pie(
        labels=["소진", "잔여"],
        values=[spent, remaining],
        hole=0.62,
        marker=dict(colors=["#ef4444", "#e5e7eb"], line=dict(color="white", width=2)),
        textinfo="none",
        sort=False, direction="clockwise",
    )])
    fig.update_layout(
        height=height,
        margin=dict(t=10, b=10, l=10, r=10),
        showlegend=True,
        legend=dict(orientation="v", x=1.02, y=0.5, font=dict(size=11)),
        annotations=[dict(
            text=f"<b style='font-size:26px'>{burn:.1f}%</b><br><span style='font-size:11px;color:#666'>소진율</span>",
            x=0.5, y=0.5, showarrow=False
        )]
    )
    return fig

def render_kpi(df, total_budget=0, show_conversion=True, key_suffix=""):
    tot_imp = int(df["impressions"].sum()); tot_clk = int(df["clicks"].sum())
    tot_cost = float(df["cost"].sum()); tot_conv = float(df["conversions"].sum())
    ctr = safe_div(tot_clk, tot_imp)*100; cpm = safe_div(tot_cost, tot_imp)*1000
    cpc = safe_div(tot_cost, tot_clk); cpa = safe_div(tot_cost, tot_conv)
    labels = sorted(set(df["conv_label"].dropna().unique()))
    conv_label = "/".join(labels) if labels else "CPA"

    if total_budget > 0:
        col_d, col_m = st.columns([1, 2])
        with col_d:
            st.plotly_chart(
                render_budget_donut(tot_cost, total_budget),
                use_container_width=True,
                key=f"budget_donut_{key_suffix}_{tot_cost}_{total_budget}"
            )
        with col_m:
            r1 = st.columns(3)
            r1[0].metric("총 예산", f"₩{total_budget:,.0f}")
            r1[1].metric("소진 광고비", f"₩{tot_cost:,.0f}")
            r1[2].metric("잔여 예산", f"₩{max(total_budget - tot_cost, 0):,.0f}")
            if show_conversion:
                r2 = st.columns(3)
                r2[0].metric("노출", f"{tot_imp:,}")
                r2[1].metric("클릭", f"{tot_clk:,}")
                r2[2].metric(f"전환 ({conv_label})", f"{tot_conv:,.0f}")
            else:
                r2 = st.columns(2)
                r2[0].metric("노출", f"{tot_imp:,}")
                r2[1].metric("클릭", f"{tot_clk:,}")
        if show_conversion:
            r3 = st.columns(4)
            r3[0].metric("CTR", f"{ctr:.2f}%")
            r3[1].metric("CPM", f"₩{cpm:,.0f}")
            r3[2].metric("CPC", f"₩{cpc:,.0f}")
            r3[3].metric(conv_label, f"₩{cpa:,.0f}" if tot_conv else "—")
        else:
            r3 = st.columns(3)
            r3[0].metric("CTR", f"{ctr:.2f}%")
            r3[1].metric("CPM", f"₩{cpm:,.0f}")
            r3[2].metric("CPC", f"₩{cpc:,.0f}")
    else:
        if show_conversion:
            c = st.columns(4)
            c[0].metric("광고비", f"₩{tot_cost:,.0f}")
            c[1].metric("노출", f"{tot_imp:,}")
            c[2].metric("클릭", f"{tot_clk:,}")
            c[3].metric(f"전환 ({conv_label})", f"{tot_conv:,.0f}")
            c2 = st.columns(4)
            c2[0].metric("CTR", f"{ctr:.2f}%")
            c2[1].metric("CPM", f"₩{cpm:,.0f}")
            c2[2].metric("CPC", f"₩{cpc:,.0f}")
            c2[3].metric(conv_label, f"₩{cpa:,.0f}" if tot_conv else "—")
        else:
            c = st.columns(3)
            c[0].metric("광고비", f"₩{tot_cost:,.0f}")
            c[1].metric("노출", f"{tot_imp:,}")
            c[2].metric("클릭", f"{tot_clk:,}")
            c2 = st.columns(3)
            c2[0].metric("CTR", f"{ctr:.2f}%")
            c2[1].metric("CPM", f"₩{cpm:,.0f}")
            c2[2].metric("CPC", f"₩{cpc:,.0f}")
    return conv_label

# ⭐ 신규: 지표 컬럼 추가 헬퍼
def _add_metric_cols(g, conv_label, show_conversion=True):
    g = g.copy()
    g["CTR (%)"] = g.apply(lambda r: round(safe_div(r["clicks"],r["impressions"])*100,2), axis=1)
    g["CPM (₩)"] = g.apply(lambda r: round(safe_div(r["cost"],r["impressions"])*1000), axis=1)
    g["CPC (₩)"] = g.apply(lambda r: round(safe_div(r["cost"],r["clicks"])), axis=1)
    if show_conversion:
        g[f"{conv_label} (₩)"] = g.apply(
            lambda r: round(safe_div(r["cost"],r["conversions"])) if r["conversions"] else 0, axis=1)
    g["광고비"] = g["cost"].astype(int)
    g["노출"] = g["impressions"]
    g["클릭"] = g["clicks"]
    if show_conversion:
        g["전환"] = g["conversions"].astype(int)
    return g

def render_campaign_table(df, conv_label, key, show_conversion=True):
    unit = st.radio("집계 단위", ["캠페인 합계", "일자별"],
                    horizontal=True, key=f"{key}_unit")
    base_cols = ["노출","클릭","광고비"]
    metric_cols = ["CTR (%)","CPM (₩)","CPC (₩)"]
    if show_conversion:
        base_cols.append("전환")
        metric_cols.append(f"{conv_label} (₩)")
    if unit == "캠페인 합계":
        g = df.groupby("campaign", as_index=False).agg(
            impressions=("impressions","sum"), clicks=("clicks","sum"),
            cost=("cost","sum"), conversions=("conversions","sum"))
        g = _add_metric_cols(g, conv_label, show_conversion)
        show = g[["campaign"] + base_cols + metric_cols]
        show = show.rename(columns={"campaign":"캠페인"}).sort_values("광고비", ascending=False)
    else:
        g = df.groupby(["date","campaign"], as_index=False).agg(
            impressions=("impressions","sum"), clicks=("clicks","sum"),
            cost=("cost","sum"), conversions=("conversions","sum"))
        g = _add_metric_cols(g, conv_label, show_conversion)
        g["일자"] = pd.to_datetime(g["date"]).dt.strftime("%Y-%m-%d")
        show = g[["일자","campaign"] + base_cols + metric_cols]
        show = show.rename(columns={"campaign":"캠페인"}).sort_values(
            ["일자","광고비"], ascending=[True, False])
    for col in show.columns:
        if col in ["노출","클릭","전환"]:
            show[col] = show[col].apply(lambda x: f"{int(x):,}")
        elif col == "광고비" or "(₩)" in col:
            show[col] = show[col].apply(lambda x: f"₩{int(x):,}")
            
    st.dataframe(show, use_container_width=True, hide_index=True)

def render_adgroup_table(df, conv_label, key, show_conversion=True):
    unit = st.radio("집계 단위", ["광고그룹 합계", "일자별"],
                    horizontal=True, key=f"{key}_unit")
    base_cols = ["노출","클릭","광고비"]
    metric_cols = ["CTR (%)","CPM (₩)","CPC (₩)"]
    if show_conversion:
        base_cols.append("전환")
        metric_cols.append(f"{conv_label} (₩)")
    if unit == "광고그룹 합계":
        g = df.groupby("adgroup", as_index=False).agg(
            impressions=("impressions","sum"), clicks=("clicks","sum"),
            cost=("cost","sum"), conversions=("conversions","sum"))
        g = _add_metric_cols(g, conv_label, show_conversion)
        show = g[["adgroup"] + base_cols + metric_cols]
        show = show.rename(columns={"adgroup":"광고그룹"}).sort_values("광고비", ascending=False)
    else:
        g = df.groupby(["date","adgroup"], as_index=False).agg(
            impressions=("impressions","sum"), clicks=("clicks","sum"),
            cost=("cost","sum"), conversions=("conversions","sum"))
        g = _add_metric_cols(g, conv_label, show_conversion)
        g["일자"] = pd.to_datetime(g["date"]).dt.strftime("%Y-%m-%d")
        show = g[["일자","adgroup"] + base_cols + metric_cols]
        show = show.rename(columns={"adgroup":"광고그룹"}).sort_values(
            ["일자","광고비"], ascending=[True, False])
    for col in show.columns:
        if col in ["노출","클릭","전환"]:
            show[col] = show[col].apply(lambda x: f"{int(x):,}")
        elif col == "광고비" or "(₩)" in col:
            show[col] = show[col].apply(lambda x: f"₩{int(x):,}")
    st.dataframe(show, use_container_width=True, hide_index=True)

# ============ 퍼널 분석 헬퍼 ============
def get_raw_data_columns(adv_code, platform):
    """업로드된 raw_data에서 사용 가능한 숫자 컬럼 목록 추출"""
    rows = q("""SELECT raw_data FROM perf
                WHERE advertiser_code=%s AND platform=%s AND raw_data IS NOT NULL
                ORDER BY id DESC LIMIT 100""", (adv_code, platform))
    cols = set()
    for r in rows:
        try:
            d = json.loads(r[0])
            for k, v in d.items():
                try: float(v); cols.add(k)
                except: pass
        except: pass
    return sorted(cols)

def _ensure_funnel_table():
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS funnel_mapping (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        advertiser_code TEXT NOT NULL,
        platform TEXT NOT NULL,
        step_order INTEGER NOT NULL,
        column_name TEXT NOT NULL,
        label TEXT NOT NULL,
        cvr_base TEXT DEFAULT 'clicks'
    )""")
    con.commit(); con.close()

def get_funnel_steps(adv_code, platform):
    _ensure_funnel_table()
    rows = q("""SELECT step_order, column_name, label, COALESCE(cvr_base,'clicks')
                FROM funnel_mapping WHERE advertiser_code=%s AND platform=%s
                ORDER BY step_order""", (adv_code, platform))
    return [{"order": r[0], "column": r[1], "label": r[2], "cvr_base": r[3]} for r in rows]
def save_funnel_steps(adv_code, platform, steps):
    _ensure_funnel_table()
    q("DELETE FROM funnel_mapping WHERE advertiser_code=%s AND platform=%s",
      (adv_code, platform), fetch=False)
    for i, s in enumerate(steps, 1):
        q("""INSERT INTO funnel_mapping
             (advertiser_code,platform,step_order,column_name,label,cvr_base)
             VALUES (%s,%s,%s,%s,%s,%s)""",
          (adv_code, platform, i, s["column"], s["label"], s["cvr_base"]), fetch=False)
        
def render_funnel_table(df, funnel_steps, group_by="overall", key=""):
    """퍼널 단계별 CPA/CVR 표 렌더링"""
    if df.empty:
        st.info("데이터가 없습니다."); return
    if not funnel_steps:
        st.info("설정된 퍼널 단계가 없습니다."); return
    
    df = df.copy()
    parsed = df["raw_data"].fillna("{}").apply(
        lambda x: json.loads(x) if isinstance(x, str) and x else {})
    
    sorted_steps = sorted(funnel_steps, key=lambda x: x["order"])
    for step in sorted_steps:
        col = step["column"]
        df[f"_s{step['order']}"] = parsed.apply(
            lambda d, c=col: float(d.get(c, 0) or 0))
    
    # 그룹 정의
    if group_by == "campaign":
        df["_grp"] = df["campaign"]; grp_label = "캠페인"
    elif group_by == "adgroup":
        df["_grp"] = df["campaign"].astype(str) + " > " + df["adgroup"].astype(str)
        grp_label = "캠페인 > 광고그룹"
    elif group_by == "creative":
        df = df[df["creative"].notna() & (df["creative"] != "") & (df["creative"] != "None")]
        if df.empty:
            st.info("소재 데이터가 없습니다."); return
        df["_grp"] = df["creative"]; grp_label = "소재"
    else:
        df["_grp"] = "전체"; grp_label = "구분"
    
    agg = {"impressions":"sum", "clicks":"sum", "cost":"sum"}
    for step in sorted_steps:
        agg[f"_s{step['order']}"] = "sum"
    g = df.groupby("_grp", as_index=False).agg(agg).sort_values("cost", ascending=False)
    
    # Total 행 (그룹이 1개 초과일 때만)
    if len(g) > 1:
        total = {c: g[c].sum() for c in g.columns if c != "_grp"}
        total["_grp"] = "Total"
        g = pd.concat([pd.DataFrame([total]), g], ignore_index=True)
    
    out = pd.DataFrame()
    out[grp_label] = g["_grp"]
    out["Imp"] = g["impressions"].astype(int).map("{:,}".format)
    out["Click"] = g["clicks"].astype(int).map("{:,}".format)
    out["CTR"] = (g["clicks"] / g["impressions"].replace(0, 1) * 100).round(2).map("{:.2f}%".format)
    out["CPC"] = (g["cost"] / g["clicks"].replace(0, 1)).round().astype(int).map("₩{:,}".format)
    out["COST"] = g["cost"].astype(int).map("₩{:,}".format)
    
    for i, step in enumerate(sorted_steps):
        scol = f"_s{step['order']}"
        label = step["label"]
        cvr_base = step.get("cvr_base", "clicks")
        
        out[label] = g[scol].astype(int).map("{:,}".format)
        cpa = (g["cost"] / g[scol].replace(0, 1)).round().astype(int)
        out[f"CPA·{label}"] = [f"₩{x:,}" if cnt > 0 else "—"
                                for x, cnt in zip(cpa, g[scol])]
        
        if cvr_base == "previous" and i > 0:
            prev = sorted_steps[i-1]
            base = g[f"_s{prev['order']}"]
            cvr_label = f"CVR·{label}(↑{prev['label']}대비)"
        else:
            base = g["clicks"]
            cvr_label = f"CVR·{label}"
        cvr = (g[scol] / base.replace(0, 1) * 100).round(2)
        out[cvr_label] = [f"{x:.2f}%" if b > 0 else "—" for x, b in zip(cvr, base)]
    
    st.dataframe(out, use_container_width=True, hide_index=True)
    
    # CSV 다운로드
    csv_bytes = out.to_csv(index=False).encode("utf-8-sig")
    st.download_button("📥 CSV 다운로드", data=csv_bytes,
        file_name=f"funnel_{key}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv", key=f"{key}_dl")

# ============ 광고 소재 탭 ============
def render_creative_tab(df_pf, platform, key_prefix, show_conv=True):
    """광고 소재별 성과 분석 화면"""
    df_cre = df_pf[df_pf["creative"].notna() & (df_pf["creative"] != "") & (df_pf["creative"] != "None")]
    if df_cre.empty:
        st.info(f"💡 {platform} 매체에 광고 소재 데이터가 없습니다.\n\n"
                f"데이터 업로드 시 '🎨 광고 소재 컬럼'을 매핑한 뒤 다시 시도해주세요.")
        return
    
    st.caption("🎨 광고 이미지·영상·텍스트별 성과를 분석합니다. "
               "기본은 전체 데이터 기준이며, 필요 시 캠페인/광고그룹으로 필터링하세요.")
    
    # ===== 필터 영역 =====
    with st.container():
        fc1, fc2 = st.columns(2)
        with fc1:
            all_camps = ["(전체)"] + sorted(df_cre["campaign"].unique().tolist())
            sel_camp = st.selectbox("📁 캠페인 필터", all_camps, key=f"{key_prefix}_camp")
        with fc2:
            if sel_camp == "(전체)":
                ag_pool = df_cre
            else:
                ag_pool = df_cre[df_cre["campaign"] == sel_camp]
            all_ags = ["(전체)"] + sorted(ag_pool["adgroup"].unique().tolist())
            sel_ag = st.selectbox("📂 광고그룹 필터", all_ags, key=f"{key_prefix}_ag")
    
    df_f = df_cre.copy()
    if sel_camp != "(전체)":
        df_f = df_f[df_f["campaign"] == sel_camp]
    if sel_ag != "(전체)":
        df_f = df_f[df_f["adgroup"] == sel_ag]
    
    if df_f.empty:
        st.warning("선택한 조건에 해당하는 소재가 없습니다."); return
    
    # ===== KPI =====
    n_creatives = df_f["creative"].nunique()
    tot_imp = int(df_f["impressions"].sum())
    tot_clk = int(df_f["clicks"].sum())
    tot_cost = float(df_f["cost"].sum())
    tot_conv = float(df_f["conversions"].sum()) if "conversions" in df_f.columns else 0
    labels = sorted(set(df_f["conv_label"].dropna().unique())) if "conv_label" in df_f.columns else []
    conv_label = "/".join(labels) if labels else "CPA"
    
    k = st.columns(5 if show_conv else 4)
    k[0].metric("소재 수", f"{n_creatives:,}")
    k[1].metric("노출", f"{tot_imp:,}")
    k[2].metric("클릭", f"{tot_clk:,}")
    k[3].metric("광고비", f"₩{tot_cost:,.0f}")
    if show_conv:
        k[4].metric(f"전환 ({conv_label})", f"{tot_conv:,.0f}")
    
    st.divider()
    
    # ===== 소재별 성과 표 =====
    st.subheader("🎨 소재별 성과")
    g = df_f.groupby("creative", as_index=False).agg(
        impressions=("impressions","sum"), clicks=("clicks","sum"),
        cost=("cost","sum"), conversions=("conversions","sum") if show_conv else ("clicks","sum"))
    g["CTR (%)"] = g.apply(lambda r: round(safe_div(r["clicks"], r["impressions"])*100, 2), axis=1)
    g["CPM (₩)"] = g.apply(lambda r: round(safe_div(r["cost"], r["impressions"])*1000), axis=1)
    g["CPC (₩)"] = g.apply(lambda r: round(safe_div(r["cost"], r["clicks"])), axis=1)
    g["광고비"] = g["cost"].astype(int)
    g["노출"] = g["impressions"]; g["클릭"] = g["clicks"]
    
    base_cols = ["creative","노출","클릭","광고비","CTR (%)","CPM (₩)","CPC (₩)"]
    if show_conv:
        g["전환"] = g["conversions"].astype(int)
        g[f"CVR (%)"] = g.apply(lambda r: round(safe_div(r["conversions"], r["clicks"])*100, 2), axis=1)
        g[f"{conv_label} (₩)"] = g.apply(
            lambda r: round(safe_div(r["cost"], r["conversions"])) if r["conversions"] else 0, axis=1)
        cols_show = base_cols + ["전환","CVR (%)",f"{conv_label} (₩)"]
    else:
        cols_show = base_cols
    
    show = g[cols_show].rename(columns={"creative":"소재"}).sort_values("광고비", ascending=False)
    for col in show.columns:
        if col in ["노출","클릭","전환"]:
            show[col] = show[col].apply(lambda x: f"{int(x):,}")
        elif col == "광고비" or "(₩)" in col:
            show[col] = show[col].apply(lambda x: f"₩{int(x):,}") 
    st.dataframe(show, use_container_width=True, hide_index=True)
    
    st.divider()
    
    # ===== 차트 =====
    cc1, cc2 = st.columns(2)
    with cc1:
        st.subheader("💰 소재별 광고비 TOP 15")
        top_cost = g.sort_values("cost", ascending=False).head(15)
        if not top_cost.empty:
            fig = px.bar(top_cost, x="cost", y="creative", orientation="h",
                         color_discrete_sequence=["#4285F4" if platform=="GOOGLE" else "#1877F2"])
            fig.update_layout(height=420, yaxis={"categoryorder":"total ascending"},
                              showlegend=False, margin=dict(l=10, r=10, t=20, b=20))
            st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_cost_chart")
    with cc2:
        st.subheader("🎯 소재별 CTR TOP 15")
        # 노출 100 미만은 노이즈로 제외
        top_ctr = g[g["impressions"] >= 100].sort_values("CTR (%)", ascending=False).head(15)
        if not top_ctr.empty:
            fig = px.bar(top_ctr, x="CTR (%)", y="creative", orientation="h",
                         color_discrete_sequence=["#10B981"])
            fig.update_layout(height=420, yaxis={"categoryorder":"total ascending"},
                              showlegend=False, margin=dict(l=10, r=10, t=20, b=20))
            st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_ctr_chart")
        else:
            st.caption("노출 100회 이상인 소재가 없습니다.")
    
    # ===== 일자별 비교 (소재 선택) =====
    st.divider()
    st.subheader("📈 일자별 소재 성과 비교")
    cre_options = g.sort_values("cost", ascending=False)["creative"].tolist()
    default_cre = cre_options[:min(5, len(cre_options))]
    sel_cres = st.multiselect("비교할 소재 선택 (최대 권장 10개)",
                              cre_options, default=default_cre, key=f"{key_prefix}_msel")
    metric_pick = st.radio("지표", ["광고비","노출","클릭","CTR (%)"],
                           horizontal=True, key=f"{key_prefix}_metric")
    if sel_cres:
        df_d = df_f[df_f["creative"].isin(sel_cres)]
        daily = df_d.groupby(["date","creative"], as_index=False).agg(
            impressions=("impressions","sum"), clicks=("clicks","sum"),
            cost=("cost","sum"))
        daily["CTR (%)"] = daily.apply(lambda r: round(safe_div(r["clicks"], r["impressions"])*100, 2), axis=1)
        daily = daily.rename(columns={"cost":"광고비","impressions":"노출","clicks":"클릭"})
        fig = px.line(daily, x="date", y=metric_pick, color="creative", markers=True)
        fig.update_layout(height=400, hovermode="x unified",
                          margin=dict(l=10, r=10, t=20, b=20))
        st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_daily_chart")

# ============ 대시보드 ============
if page == "📈 대시보드" and adv_code:
    st.title(f"📈 {sel_name} — 성과 대시보드")
    raw = pd.read_sql("SELECT * FROM perf WHERE advertiser_code=%s", sqlite3.connect(DB), params=(adv_code,))
    if raw.empty:
        st.warning("데이터가 없습니다. '데이터 업로드' 메뉴에서 파일을 올려주세요."); st.stop()
    raw["date"] = pd.to_datetime(raw["date"])
    min_d, max_d = raw["date"].min().date(), raw["date"].max().date()

    adv_row = q("SELECT total_budget, COALESCE(show_conversion,1) FROM advertisers WHERE code=%s", (adv_code,))
    if adv_row:
        total_budget = float(adv_row[0][0] or 0)
        show_conv = bool(adv_row[0][1])
    else:
        total_budget = 0; show_conv = True

    fc1, _ = st.columns([3,2])
    with fc1:
        date_range = st.date_input("📅 기간 선택", value=(min_d, max_d),
                                   min_value=min_d, max_value=max_d)

    df_all = raw.copy()
    if isinstance(date_range, tuple) and len(date_range)==2:
        d_from, d_to = date_range
        df_all = df_all[(df_all["date"]>=pd.Timestamp(d_from)) & (df_all["date"]<=pd.Timestamp(d_to))]

    mapping = get_conversion_mapping(adv_code)
    df_all = compute_metrics(df_all, mapping)

    # 동적 탭 구성: 데이터가 있는 매체만 표시
    available = sorted(df_all["platform"].unique(),
                       key=lambda x: {"GOOGLE":0,"FACEBOOK":1}.get(x,99))
    
    # 광고주별 소재 표시 옵션 조회
    cre_row = q("SELECT COALESCE(show_creative,0) FROM advertisers WHERE code=%s", (adv_code,))
    show_creative = bool(cre_row[0][0]) if cre_row else False
    
    tab_labels = ["📊 Summary"]
    tab_keys = ["summary"]
    if "GOOGLE" in available:
        tab_labels.append("🟦 Google"); tab_keys.append("google")
        if show_creative:
            tab_labels.append("🎨 구글_광고소재"); tab_keys.append("google_cre")
    if "FACEBOOK" in available:
        tab_labels.append("🟪 Facebook"); tab_keys.append("facebook")
        if show_creative:
            tab_labels.append("🎨 페이스북_광고소재"); tab_keys.append("facebook_cre")
    tabs = st.tabs(tab_labels)
    tabd = dict(zip(tab_keys, tabs))

    # Summary
    with tabd["summary"]:
        st.markdown("##### 매체 선택")
        priority = {"GOOGLE": 0, "FACEBOOK": 1, "NAVER": 2, "KAKAO": 3, "TIKTOK": 4}
        all_pfs = sorted(available, key=lambda x: priority.get(x, 99))
        if not all_pfs:
            st.info("데이터 없음")
        else:
            cb_cols = st.columns([1, 1, 1, 1, 6])
            sel_pfs = []
            for i, p in enumerate(all_pfs):
                if cb_cols[i].checkbox(p, value=True, key=f"sum_pf_{p}"):
                    sel_pfs.append(p)
            df_s = df_all[df_all["platform"].isin(sel_pfs)] if sel_pfs else df_all.iloc[0:0]
            if df_s.empty:
                st.warning("선택한 매체에 데이터가 없습니다.")
            else:
                conv_label = render_kpi(df_s, total_budget, show_conv, key_suffix="sum")
                st.divider()
                chart_daily_metric(df_s, conv_label, key_prefix="sum")
                st.divider()
                cc1, cc2 = st.columns([1, 2])
                with cc1: chart_cost_donut(df_s, "매체별 광고비 비중")
                with cc2: chart_campaign_bar(df_s, "cost", "캠페인별 광고비 TOP 10")
                st.divider()
                st.subheader("📋 캠페인별 효율")
                render_campaign_table(df_s, conv_label, key="sum_camp", show_conversion=show_conv)

    # Google
    if "google" in tabd:
        with tabd["google"]:
            df_g = df_all[df_all["platform"]=="GOOGLE"]
            conv_label = render_kpi(df_g, total_budget, show_conv, key_suffix="g")
            st.divider()
            cc1, cc2 = st.columns([2,1])
            with cc1: chart_daily_metric(df_g, conv_label, key_prefix="g")
            with cc2:
                by_c = df_g.groupby("campaign", as_index=False)["cost"].sum()
                fig = px.pie(by_c, names="campaign", values="cost", hole=0.4,
                             title="캠페인별 광고비 비중",
                             color_discrete_sequence=px.colors.sequential.Blues_r)
                fig.update_layout(height=380)
                st.plotly_chart(fig, use_container_width=True, key="g_camp_pie")
            st.divider()
            st.subheader("📋 캠페인별 성과")
            render_campaign_table(df_g, conv_label, key="g_camp", show_conversion=show_conv)
            st.divider()
            st.subheader("📁 캠페인별 광고그룹 성과")
            for camp in sorted(df_g["campaign"].unique()):
                sub = df_g[df_g["campaign"]==camp]
                with st.expander(f"📁 {camp}  (광고비 ₩{sub['cost'].sum():,.0f} · 노출 {int(sub['impressions'].sum()):,})"):
                    render_adgroup_table(sub, conv_label, key=f"g_ag_{camp}", show_conversion=show_conv)
                # 퍼널 분석
            funnel_steps_g = get_funnel_steps(adv_code, "GOOGLE")
            if funnel_steps_g:
                st.divider()
                st.subheader("🪜 퍼널 분석")
                st.caption(f"설정된 단계: {' → '.join([s['label'] for s in sorted(funnel_steps_g, key=lambda x: x['order'])])}")
                grp_g = st.radio("그룹화 기준",
                    ["전체","캠페인","광고그룹","소재"],
                    horizontal=True, key="g_funnel_grp")
                grp_map = {"전체":"overall","캠페인":"campaign","광고그룹":"adgroup","소재":"creative"}
                render_funnel_table(df_g, funnel_steps_g, group_by=grp_map[grp_g], key="g_funnel")
                    
    # Google 광고소재
    if "google_cre" in tabd:
        with tabd["google_cre"]:
            df_g = df_all[df_all["platform"]=="GOOGLE"]
            render_creative_tab(df_g, "GOOGLE", key_prefix="g_cre", show_conv=show_conv)

    # Facebook
    if "facebook" in tabd:
        with tabd["facebook"]:
            df_f = df_all[df_all["platform"]=="FACEBOOK"]
            conv_label = render_kpi(df_f, total_budget, show_conv, key_suffix="f")
            st.divider()
            cc1, cc2 = st.columns([2,1])
            with cc1: chart_daily_metric(df_f, conv_label, key_prefix="f")
            with cc2:
                by_c = df_f.groupby("campaign", as_index=False)["cost"].sum()
                fig = px.pie(by_c, names="campaign", values="cost", hole=0.4,
                             title="캠페인별 광고비 비중",
                             color_discrete_sequence=px.colors.sequential.Purples_r)
                fig.update_layout(height=380)
                st.plotly_chart(fig, use_container_width=True, key="f_camp_pie")
            st.divider()
            st.subheader("📋 캠페인별 성과")
            render_campaign_table(df_f, conv_label, key="f_camp", show_conversion=show_conv)
            st.divider()
            st.subheader("📁 캠페인별 광고그룹 성과")
            for camp in sorted(df_f["campaign"].unique()):
                sub = df_f[df_f["campaign"]==camp]
                with st.expander(f"📁 {camp}  (광고비 ₩{sub['cost'].sum():,.0f} · 노출 {int(sub['impressions'].sum()):,})"):
                    render_adgroup_table(sub, conv_label, key=f"f_ag_{camp}", show_conversion=show_conv)
            # 퍼널 분석
            funnel_steps_f = get_funnel_steps(adv_code, "FACEBOOK")
            if funnel_steps_f:
                st.divider()
                st.subheader("🪜 퍼널 분석")
                st.caption(f"설정된 단계: {' → '.join([s['label'] for s in sorted(funnel_steps_f, key=lambda x: x['order'])])}")
                grp_f = st.radio("그룹화 기준",
                    ["전체","캠페인","광고그룹","소재"],
                    horizontal=True, key="f_funnel_grp")
                grp_map = {"전체":"overall","캠페인":"campaign","광고그룹":"adgroup","소재":"creative"}
                render_funnel_table(df_f, funnel_steps_f, group_by=grp_map[grp_f], key="f_funnel")

# Facebook 광고소재
    if "facebook_cre" in tabd:
        with tabd["facebook_cre"]:
            df_f = df_all[df_all["platform"]=="FACEBOOK"]
            render_creative_tab(df_f, "FACEBOOK", key_prefix="f_cre", show_conv=show_conv)
                    
# ============ 데이터 업로드 ============
elif page == "📤 데이터 업로드" and adv_code:
    st.title("📤 로우데이터 업로드")
    if my_level == "VIEWER":
        st.error("⛔ VIEWER 권한은 업로드할 수 없습니다."); st.stop()
    
    platform = st.radio("매체 선택", ["GOOGLE","FACEBOOK"], horizontal=True, key="up_pf")
    file = st.file_uploader("파일 업로드 (xlsx / csv)", type=["xlsx","xls","csv"], key="up_file")
    
    if file:
        cur_sig = f"{file.name}_{file.size}"
        if st.session_state.get("up_sig") != cur_sig:
            st.session_state["up_sig"] = cur_sig
            for k in ["upload_df", "upload_other"]:
                if k in st.session_state: del st.session_state[k]
    
    if file:
        try:
            df_raw = read_uploaded_file(file)
            
            st.success(f"✅ 파일 읽기 완료 — {len(df_raw):,}행 · {len(df_raw.columns)}개 컬럼")
            with st.expander("📋 원본 데이터 미리보기 (상위 5행)", expanded=False):
                st.dataframe(df_raw.head(5), use_container_width=True)
            
            st.divider()
            st.subheader("🔗 컬럼 매핑")
            st.caption("각 표준 필드에 사용할 원본 컬럼을 지정하세요. 자동 추측된 값이 미리 선택되어 있습니다.")
            
            cols = ["(선택안함)"] + list(df_raw.columns)
            def safe_idx(guess):
                return cols.index(guess) if guess in cols else 0
            
            g_date = guess_column(df_raw.columns, DATE_CANDS)
            g_camp = guess_column(df_raw.columns, CAMP_CANDS)
            g_ag   = guess_column(df_raw.columns, AG_CANDS)
            g_imp  = guess_column(df_raw.columns, IMP_CANDS)
            g_clk  = guess_column(df_raw.columns, CLK_CANDS)
            g_cost = guess_column(df_raw.columns, COST_CANDS)
            g_cre  = guess_column(df_raw.columns, CREATIVE_CANDS)
            
            mc1, mc2, mc3 = st.columns(3)
            with mc1:
                col_date = st.selectbox("📅 일자 *", cols, index=safe_idx(g_date), key="map_date")
                col_imp  = st.selectbox("👁️ 노출수 *", cols, index=safe_idx(g_imp),  key="map_imp")
            with mc2:
                col_camp = st.selectbox("📁 캠페인 *", cols, index=safe_idx(g_camp), key="map_camp")
                col_clk  = st.selectbox("🖱️ 클릭수 *", cols, index=safe_idx(g_clk),  key="map_clk")
            with mc3:
                col_ag   = st.selectbox("📂 광고그룹", cols, index=safe_idx(g_ag),   key="map_ag",
                                        help="비워두면 캠페인명을 광고그룹으로 사용")
                col_cost = st.selectbox("💰 비용 *", cols, index=safe_idx(g_cost),   key="map_cost")
            
            st.markdown("##### 🎨 광고 소재 (선택)")
            scol1, scol2 = st.columns([1, 2])
            with scol1:
                use_creative = st.checkbox("소재 데이터 포함", value=bool(g_cre),
                    key="map_use_creative",
                    help="광고 이미지/영상/텍스트별 성과를 별도 탭에서 분석합니다.")
            with scol2:
                if use_creative:
                    col_cre = st.selectbox("🎨 광고 소재 컬럼", cols, index=safe_idx(g_cre),
                                           key="map_cre",
                                           label_visibility="collapsed")
                else:
                    col_cre = "(선택안함)"
            
            cost_unit = "KRW (원본 그대로)"
            if platform == "FACEBOOK":
                cost_unit = st.radio("💱 비용 통화",
                    ["KRW (원본 그대로)", "USD → KRW 환산 (× 1,300)"],
                    horizontal=True, key="map_currency",
                    index=1 if (col_cost and "USD" in col_cost.upper()) else 0)
            
            mapped = {col_date, col_camp, col_ag, col_imp, col_clk, col_cost, col_cre}
            mapped.discard("(선택안함)")
            other_numeric = []
            for c in df_raw.columns:
                if c in mapped: continue
                try:
                    pd.to_numeric(df_raw[c], errors="raise")
                    other_numeric.append(c)
                except: continue
            
            if other_numeric:
                st.caption(f"📌 raw_data에 함께 저장될 숫자 컬럼(전환 매핑 후보): "
                           f"**{', '.join(other_numeric)}**")
            
            # ====== 업로드 모드 선택 ======
            st.divider()
            st.subheader("📦 업로드 방식 선택")
            st.caption("⚠️ 잘못 선택하면 데이터가 중복되거나 사라질 수 있습니다. 설명을 꼭 읽어주세요!")
            
            mode = st.radio(
                "업로드 모드",
                ["① 추가 (Append)",
                 "② 기간 덮어쓰기 (Upsert by Date) — 권장",
                 "③ 매체 전체 초기화 (Replace All)"],
                index=1, key="up_mode")
            
            # 모드별 상세 설명
            if mode.startswith("①"):
                st.info("""
**① 추가 (Append) — 기존 데이터를 그대로 두고 새 행을 덧붙입니다**

✅ **이럴 때 사용하세요**
- 처음 데이터를 올리는 경우
- 기존에 없던 **새로운 기간**의 데이터를 누적해서 쌓을 때 (예: 1월 데이터가 이미 있고, 2월 데이터를 추가)
- 다른 광고주·다른 매체 데이터를 처음 올릴 때

⚠️ **주의 사항**
- 같은 파일을 두 번 올리면 **모든 데이터가 2배로 부풀려집니다**
- 같은 날짜가 이미 DB에 있는데 또 올리면 **중복 누적**됩니다
- 광고주가 누적 파일(1일치 → 1~2일치 → 1~3일치)을 매일 보내준다면 이 모드는 절대 쓰지 마세요
                """)
            elif mode.startswith("②"):
                st.success("""
**② 기간 덮어쓰기 (Upsert by Date) — 가장 안전한 기본 옵션 ⭐**

✅ **이럴 때 사용하세요**
- 같은 매체의 데이터를 다시 올리는 모든 경우 (대부분의 상황)
- 광고주가 **누적 파일**(1일치 → 1~2일치 → ...)을 매일 보내줄 때
- 기존 기간 데이터에 **수정/보정**이 생겨서 다시 올려야 할 때
- 어제 올렸는데 오늘 한 번 더 올리는 경우

🔄 **동작 방식**
1. 업로드한 파일에 포함된 **(매체 + 날짜)** 조합을 모두 추출
2. DB에서 그 조합에 해당하는 기존 데이터를 **모두 삭제**
3. 새 데이터를 INSERT

📌 **예시**: 7/1~7/31 데이터가 이미 DB에 있고, 7/15~8/10 파일을 올리면
   → 7/15~7/31 기존 데이터는 삭제되고 새 파일 데이터로 교체
   → 7/1~7/14 데이터는 그대로 유지
   → 8/1~8/10 데이터는 새로 추가

⚠️ **주의**: 광고주를 잘못 선택한 채 업로드하면 다른 광고주의 같은 날짜 데이터가 사라질 수 있으니, **좌측 광고주 선택**을 꼭 확인하세요.
                """)
            else:
                st.error("""
**③ 매체 전체 초기화 (Replace All) — 위험! 신중하게 사용하세요 🚨**

✅ **이럴 때 사용하세요**
- 해당 매체 데이터를 **처음부터 완전히 다시** 정리하고 싶을 때
- 잘못된 데이터가 누적되어 깨끗하게 리셋이 필요할 때
- 광고주별 / 매체별로 한 번에 통합 파일이 새로 도착한 경우

🔄 **동작 방식**
1. 현재 광고주의 **선택한 매체 데이터 전체**를 DB에서 삭제
2. 새 데이터를 INSERT

⚠️ **반드시 확인**
- 이 작업은 **되돌릴 수 없습니다**
- 다른 매체 데이터에는 영향 없음 (예: GOOGLE 모드면 FACEBOOK 데이터는 유지)
- 같은 광고주의 다른 모든 기간 데이터까지 사라집니다 (현재 매체에 한해서)

📌 **예시**: 광고주 SCONEC + 매체 GOOGLE에서 이 모드로 업로드 → SCONEC의 모든 GOOGLE 데이터 삭제 후 새 파일로 교체
                """)
            
            st.divider()
            
            if st.button("🔄 변환 실행 & 미리보기", type="secondary"):
                required = {"일자": col_date, "캠페인": col_camp,
                            "노출수": col_imp, "클릭수": col_clk, "비용": col_cost}
                missing = [k for k, v in required.items() if v == "(선택안함)"]
                if missing:
                    st.error(f"❌ 필수 항목 미지정: {', '.join(missing)}")
                else:
                    df = pd.DataFrame(index=df_raw.index)
                    df["date"] = pd.to_datetime(df_raw[col_date], errors="coerce").dt.strftime("%Y-%m-%d")
                    df["campaign"] = df_raw[col_camp].astype(str)
                    df["adgroup"] = df_raw[col_ag].astype(str) if col_ag != "(선택안함)" else df["campaign"]
                    df["impressions"] = pd.to_numeric(df_raw[col_imp], errors="coerce").fillna(0).astype(int)
                    df["clicks"] = pd.to_numeric(df_raw[col_clk], errors="coerce").fillna(0).astype(int)
                    df["cost"] = pd.to_numeric(df_raw[col_cost], errors="coerce").fillna(0)
                    if cost_unit.startswith("USD"):
                        df["cost"] = df["cost"] * 1300
                    df["creative"] = df_raw[col_cre].astype(str) if col_cre != "(선택안함)" else None

                    
                    def make_raw(idx):
                        d = {}
                        for c in other_numeric:
                            v = df_raw.loc[idx, c]
                            try: d[c] = float(v) if pd.notna(v) else 0
                            except: d[c] = 0
                        return json.dumps(d, ensure_ascii=False)
                    df["raw_data"] = [make_raw(i) for i in df.index]
                    df = df.dropna(subset=["date"])
                    
                    if df.empty:
                        st.error("❌ 일자 컬럼이 인식되지 않았습니다. 다른 컬럼을 선택해주세요.")
                    else:
                        st.session_state["upload_df"] = df.reset_index(drop=True)
                        st.session_state["upload_other"] = other_numeric
                        st.success(f"✅ 변환 완료 — {len(df):,}행")
            
            if "upload_df" in st.session_state:
                df = st.session_state["upload_df"]
                st.subheader("📊 변환된 데이터 미리보기")
                st.dataframe(df.head(8), use_container_width=True, hide_index=True)
                
                kc = st.columns(4)
                kc[0].metric("총 행수", f"{len(df):,}")
                kc[1].metric("총 노출", f"{int(df['impressions'].sum()):,}")
                kc[2].metric("총 클릭", f"{int(df['clicks'].sum()):,}")
                kc[3].metric("총 비용", f"₩{float(df['cost'].sum()):,.0f}")
                
                if st.session_state.get("upload_other"):
                    st.info(f"💡 감지된 전환 후보 컬럼: **{', '.join(st.session_state['upload_other'])}** "
                            f"→ '🎯 전환지표 설정' 메뉴에서 매핑하세요.")
                
                # 모드별 영향도 미리 계산해서 보여주기
                con = sqlite3.connect(DB); cur = con.cursor()
                if mode.startswith("②"):
                    dates_in_file = list(df["date"].unique())
                    placeholders = ",".join(["%s"] * len(dates_in_file))
                    will_delete = cur.execute(
                        f"SELECT COUNT(*) FROM perf WHERE advertiser_code=%s AND platform=%s AND date IN ({placeholders})",
                        (adv_code, platform, *dates_in_file)).fetchone()[0]
                    st.warning(f"⚠️ 저장 시 기존 **{will_delete:,}행**(매체 {platform}, "
                               f"{min(dates_in_file)}~{max(dates_in_file)})이 삭제되고 "
                               f"새 **{len(df):,}행**으로 교체됩니다.")
                elif mode.startswith("③"):
                    will_delete = cur.execute(
                        "SELECT COUNT(*) FROM perf WHERE advertiser_code=%s AND platform=%s",
                        (adv_code, platform)).fetchone()[0]
                    st.error(f"🚨 저장 시 현재 광고주의 **{platform} 매체 전체 {will_delete:,}행**이 "
                             f"모두 삭제되고 새 **{len(df):,}행**으로 교체됩니다. 신중히 진행하세요!")
                con.close()
                
                # 모드 ③은 추가 확인 절차
                proceed = True
                if mode.startswith("③"):
                    confirm_text = st.text_input(
                        f"매체 전체 초기화를 진행하려면 **{platform}** 을(를) 그대로 입력하세요",
                        key="confirm_replace")
                    proceed = (confirm_text.strip().upper() == platform)
                    if not proceed and confirm_text:
                        st.warning("입력값이 일치하지 않습니다.")
                
                btn_label = {"①":"💾 추가 저장", "②":"💾 덮어쓰기 저장", "③":"🚨 초기화 후 저장"}[mode[0]]
                btn_type = "primary" if proceed else "secondary"
                
                if st.button(btn_label, type=btn_type, disabled=not proceed):
                    con = sqlite3.connect(DB); cur = con.cursor()
                    deleted = 0
                    
                    if mode.startswith("②"):
                        dates_in_file = list(df["date"].unique())
                        placeholders = ",".join(["%s"] * len(dates_in_file))
                        cur.execute(
                            f"DELETE FROM perf WHERE advertiser_code=%s AND platform=%s AND date IN ({placeholders})",
                            (adv_code, platform, *dates_in_file))
                        deleted = cur.rowcount
                    elif mode.startswith("③"):
                        cur.execute("DELETE FROM perf WHERE advertiser_code=%s AND platform=%s",
                                    (adv_code, platform))
                        deleted = cur.rowcount
                    
                    cur.execute("""INSERT INTO upload_log
                        (email,advertiser_code,platform,file_name,rows,uploaded_at,upload_mode,deleted_rows)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (user["email"], adv_code, platform, file.name, len(df),
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         mode, deleted))
                    upload_id = cur.lastrowid
                    
                    for _, r in df.iterrows():
                        cre_val = r["creative"] if ("creative" in df.columns and pd.notna(r["creative"])) else None
                        cur.execute(
                            "INSERT INTO perf (advertiser_code,platform,date,campaign,adgroup,impressions,clicks,cost,raw_data,upload_log_id,creative) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (adv_code, platform, r["date"], r["campaign"], r["adgroup"],
                             int(r["impressions"]), int(r["clicks"]),
                             float(r["cost"]), r["raw_data"], upload_id, cre_val)
                        )
                    
                    con.commit(); con.close()
                    
                    if deleted > 0:
                        st.success(f"🎉 완료! 기존 {deleted:,}행 삭제 → 새 {len(df):,}행 저장")
                    else:
                        st.success(f"🎉 {len(df):,}행 저장 완료!")
                    
                    for k in ["upload_df", "upload_other", "up_sig"]:
                        if k in st.session_state: del st.session_state[k]
                    st.balloons()
        
        except Exception as e:
            st.error(f"파일 처리 오류: {e}")
            import traceback; st.code(traceback.format_exc())
            
# ============ 업로드 이력 ============
elif page == "📋 업로드 이력" and adv_code:
    st.title("📋 업로드 이력")
    st.caption("각 업로드 행 우측의 🗑️ 버튼으로 개별 삭제할 수 있습니다.")
    
    logs_raw = q("""SELECT id, uploaded_at, email, platform, file_name, rows,
                    COALESCE(upload_mode,'(legacy)'), COALESCE(deleted_rows,0)
                    FROM upload_log WHERE advertiser_code=%s ORDER BY id DESC""", (adv_code,))
    
    if not logs_raw:
        st.info("업로드 이력이 없습니다."); st.stop()
    
    can_delete = (my_level in ("OWNER","EDITOR")) or is_admin
    
    # 같은 (광고주, 매체) 조합에 legacy 업로드가 몇 건인지 미리 카운트
    legacy_count_per_pf = {}
    for row in logs_raw:
        if row[6] == '(legacy)':
            legacy_count_per_pf[row[3]] = legacy_count_per_pf.get(row[3], 0) + 1
    
    # ===== 헤더 =====
    h = st.columns([0.7, 1.8, 2.4, 1, 2.6, 0.9, 1.4, 0.7])
    h[0].markdown("**ID**")
    h[1].markdown("**업로드 시각**")
    h[2].markdown("**사용자**")
    h[3].markdown("**매체**")
    h[4].markdown("**파일명**")
    h[5].markdown("**현재 행수**")
    h[6].markdown("**모드**")
    h[7].markdown("**삭제**")
    st.markdown("<hr style='margin:4px 0;border-color:#e5e7eb'>", unsafe_allow_html=True)
    
    # ===== 각 행 렌더링 =====
    for row in logs_raw:
        log_id, ts, email, pf, fname, rows, mode_str, del_rows = row
        
        # 현재 DB에 남아 있는 행수 계산
        if mode_str == '(legacy)':
            cur_rows = q("""SELECT COUNT(*) FROM perf
                            WHERE advertiser_code=%s AND platform=%s AND upload_log_id IS NULL""",
                         (adv_code, pf))[0][0]
            unresolvable = legacy_count_per_pf.get(pf, 0) > 1
        else:
            cur_rows = q("SELECT COUNT(*) FROM perf WHERE upload_log_id=%s", (log_id,))[0][0]
            unresolvable = False
        
        c = st.columns([0.7, 1.8, 2.4, 1, 2.6, 0.9, 1.4, 0.7])
        c[0].markdown(f"`#{log_id}`")
        c[1].markdown(f"<span style='font-size:13px'>{ts}</span>", unsafe_allow_html=True)
        c[2].markdown(f"<span style='font-size:13px'>{email}</span>", unsafe_allow_html=True)
        c[3].markdown(f"`{pf}`")
        fname_short = fname if len(fname) <= 28 else fname[:25] + "..."
        c[4].markdown(f"<span style='font-size:13px' title='{fname}'>{fname_short}</span>",
                      unsafe_allow_html=True)
        c[5].markdown(f"**{cur_rows:,}**")
        mode_short = mode_str.replace("(Append)", "").replace("(Upsert by Date)", "") \
                             .replace("(Replace All)", "").replace(" — 권장", "").strip()
        c[6].markdown(f"<span style='font-size:12px'>{mode_short}</span>", unsafe_allow_html=True)
        
        if can_delete:
            if c[7].button("🗑️", key=f"del_btn_{log_id}",
                           help="이 업로드 데이터를 삭제합니다"):
                st.session_state["pending_delete"] = log_id
                st.rerun()
        else:
            c[7].markdown("—")
    
    if not can_delete:
        st.caption("ℹ️ 삭제는 OWNER / EDITOR 권한자만 가능합니다.")
        st.stop()
    
    # ===== 삭제 확인 다이얼로그 (선택된 경우) =====
    pid = st.session_state.get("pending_delete")
    if pid:
        sel = next((r for r in logs_raw if r[0] == pid), None)
        if not sel:
            st.session_state.pop("pending_delete", None)
            st.rerun()
        
        log_id, ts, email, pf, fname, rows, mode_str, _ = sel
        
        if mode_str == '(legacy)':
            cur_rows = q("""SELECT COUNT(*) FROM perf
                            WHERE advertiser_code=%s AND platform=%s AND upload_log_id IS NULL""",
                         (adv_code, pf))[0][0]
            unresolvable = legacy_count_per_pf.get(pf, 0) > 1
        else:
            cur_rows = q("SELECT COUNT(*) FROM perf WHERE upload_log_id=%s", (log_id,))[0][0]
            unresolvable = False
        
        st.divider()
        st.markdown(
            f"""<div style='background:#fef3c7;border-left:4px solid #f59e0b;
            padding:14px 16px;border-radius:6px'>
            <strong>🗑️ 삭제 확인 — 업로드 #{log_id}</strong><br><br>
            <ul style='margin:0;padding-left:20px;font-size:14px'>
              <li>업로드 시각: <code>{ts}</code></li>
              <li>업로더: <code>{email}</code></li>
              <li>매체: <code>{pf}</code> · 파일명: <code>{fname}</code></li>
              <li>등록 시 저장행수: <code>{rows:,}행</code></li>
              <li><strong>현재 DB 잔여 행수: {cur_rows:,}행</strong></li>
            </ul></div>""",
            unsafe_allow_html=True)
        
        if mode_str == '(legacy)' and unresolvable:
            st.error(f"""⚠️ **자동 삭제 불가**

이 광고주의 **{pf}** 매체에 추적 불가(legacy) 업로드가 **여러 건** 있어,
어떤 업로드의 데이터인지 자동 구분할 수 없습니다.

**해결 방법 (택 1)**:
1. **데이터 업로드** 메뉴 → **③ 매체 전체 초기화** 모드로 새 파일 업로드 (기존 legacy 데이터 모두 삭제됨)
2. **광고주 관리** → 광고주 삭제 후 재등록
3. 아래 **이력 레코드만 삭제** 버튼으로 이력만 정리 (실제 데이터는 그대로)
""")
            ec1, ec2, _ = st.columns([1.2, 1, 4])
            with ec1:
                if st.button("📝 이력 레코드만 삭제", key=f"legacy_log_only_{pid}"):
                    q("DELETE FROM upload_log WHERE id=%s", (pid,), fetch=False)
                    st.session_state.pop("pending_delete", None)
                    st.success("이력 레코드 삭제 완료"); st.rerun()
            with ec2:
                if st.button("❌ 취소", key=f"legacy_cancel_{pid}"):
                    st.session_state.pop("pending_delete", None); st.rerun()
        else:
            if cur_rows == 0:
                st.info("💡 데이터는 이미 비어있습니다. 이력 레코드만 제거됩니다.")
            cc1, cc2, _ = st.columns([1, 1, 4])
            with cc1:
                if st.button("✅ 삭제 확정", type="primary", key=f"confirm_{pid}"):
                    con = sqlite3.connect(DB); cur = con.cursor()
                    if mode_str == '(legacy)':
                        cur.execute("""DELETE FROM perf WHERE advertiser_code=%s AND platform=%s
                                       AND upload_log_id IS NULL""", (adv_code, pf))
                    else:
                        cur.execute("DELETE FROM perf WHERE upload_log_id=%s", (log_id,))
                    deleted = cur.rowcount
                    cur.execute("DELETE FROM upload_log WHERE id=%s", (log_id,))
                    con.commit(); con.close()
                    st.session_state.pop("pending_delete", None)
                    st.success(f"✅ {deleted:,}행 + 이력 삭제 완료")
                    st.rerun()
            with cc2:
                if st.button("❌ 취소", key=f"cancel_{pid}"):
                    st.session_state.pop("pending_delete", None); st.rerun()
                    
# ============ 전환지표 설정 ============
elif page == "🎯 전환지표 설정" and adv_code:
    st.title("🎯 전환지표 매핑 설정")
    st.caption("캠페인 성격에 따라 어떤 컬럼을 '전환수'로 쓸지, 어떤 라벨(CPI/CPA)로 표시할지 지정합니다.")
    cur_map = pd.read_sql("""SELECT platform AS 매체, campaign AS 캠페인,
                             conversion_column AS 전환컬럼, conversion_label AS 라벨,
                             updated_at AS 수정시각 FROM conversion_mapping
                             WHERE advertiser_code=%s ORDER BY platform, campaign""",
                          sqlite3.connect(DB), params=(adv_code,))
    st.subheader("📌 현재 매핑")
    if cur_map.empty: st.info("아직 매핑이 없습니다.")
    else: st.dataframe(cur_map, use_container_width=True, hide_index=True)
    st.divider()
    if my_level in ("OWNER","EDITOR") or is_admin:
        st.subheader("➕ 매핑 추가/수정")
        raw = pd.read_sql("SELECT platform, campaign, raw_data FROM perf WHERE advertiser_code=%s",
                          sqlite3.connect(DB), params=(adv_code,))
        c1, c2 = st.columns(2)
        with c1: sel_pf = st.selectbox("매체", ["GOOGLE","FACEBOOK"])
        with c2:
            camps = ["* (이 매체의 기본값)"] + sorted(raw[raw["platform"]==sel_pf]["campaign"].dropna().unique().tolist())
            sel_camp = st.selectbox("캠페인", camps)
            sel_camp_val = "*" if sel_camp.startswith("*") else sel_camp
        conv_keys = set()
        for rd in raw[raw["platform"]==sel_pf]["raw_data"].dropna():
            try: conv_keys.update(json.loads(rd).keys())
            except: pass
        conv_keys = sorted(conv_keys)
        if not conv_keys:
            st.warning(f"{sel_pf} 데이터가 없거나 전환 후보 컬럼이 없습니다.")
        else:
            c3, c4 = st.columns(2)
            with c3: sel_col = st.selectbox("전환으로 사용할 컬럼", conv_keys)
            with c4: sel_lbl = st.selectbox("표시 라벨", ["CPI","CPA","CPL","CPV","CPE"])
            if st.button("💾 저장", type="primary"):
                q("""INSERT INTO conversion_mapping (advertiser_code,platform,campaign,conversion_column,conversion_label,updated_at)
                     VALUES (%s,%s,%s,%s,%s,%s)
                     ON CONFLICT(advertiser_code,platform,campaign) DO UPDATE SET
                       conversion_column=excluded.conversion_column,
                       conversion_label=excluded.conversion_label,
                       updated_at=excluded.updated_at""",
                  (adv_code, sel_pf, sel_camp_val, sel_col, sel_lbl,
                   datetime.now().strftime("%Y-%m-%d %H:%M:%S")), fetch=False)
                st.success("저장됨"); st.rerun()
        st.divider()
        st.subheader("🗑️ 매핑 삭제")
        if not cur_map.empty:
            del_idx = st.selectbox("삭제할 매핑", cur_map.index,
                format_func=lambda i: f"{cur_map.iloc[i]['매체']} / {cur_map.iloc[i]['캠페인']} → {cur_map.iloc[i]['전환컬럼']}({cur_map.iloc[i]['라벨']})")
            if st.button("삭제"):
                row = cur_map.iloc[del_idx]
                q("DELETE FROM conversion_mapping WHERE advertiser_code=%s AND platform=%s AND campaign=%s",
                  (adv_code, row["매체"], row["캠페인"]), fetch=False)
                st.success("삭제됨"); st.rerun()

    # ===== 다단계 퍼널 설정 =====
    st.divider()
    st.title("🪜 퍼널 단계 설정")
    st.markdown("""
**다단계 전환을 한 표에서 분석할 수 있게 단계를 정의합니다.**  
예: 랜딩페이지 조회 → 장바구니 담기 → 결제 시작 → 구매  

- 매체별로 따로 설정합니다 (Google과 Facebook의 컬럼명이 다르기 때문)
- 각 단계는 `데이터 업로드` 시 매핑되지 않은 숫자 컬럼들 중에서 선택할 수 있습니다
- **CVR 기준**: `클릭 대비` = 항상 클릭수 기준 / `이전 단계 대비` = 직전 퍼널 단계 기준
""")
    
    fpf = st.radio("매체 선택", ["GOOGLE","FACEBOOK"], horizontal=True, key="funnel_pf")
    avail_cols = get_raw_data_columns(adv_code, fpf)
    
    if not avail_cols:
        st.info(f"💡 {fpf} 매체에 raw_data 컬럼이 없습니다.\n\n"
                f"먼저 `데이터 업로드`에서 전환 후보 컬럼이 포함된 파일을 올려주세요.")
    else:
        st.caption(f"📌 사용 가능한 컬럼: {', '.join(avail_cols)}")
        
        sk = f"funnel_steps_{adv_code}_{fpf}"
        if sk not in st.session_state:
            cur_steps = get_funnel_steps(adv_code, fpf)
            st.session_state[sk] = [
                {"column": s["column"], "label": s["label"], "cvr_base": s["cvr_base"]}
                for s in cur_steps
            ]
        
        steps = st.session_state[sk]
        
        # 헤더
        if steps:
            h = st.columns([0.4, 2.5, 2.2, 1.6, 0.5])
            h[0].markdown("**#**")
            h[1].markdown("**컬럼**")
            h[2].markdown("**라벨 (표에 표시될 이름)**")
            h[3].markdown("**CVR 기준**")
            h[4].markdown("**삭제**")
        
        # 단계별 행
        new_steps = []
        for i, step in enumerate(steps):
            cr = st.columns([0.4, 2.5, 2.2, 1.6, 0.5])
            cr[0].markdown(f"**{i+1}**")
            sel_col = cr[1].selectbox(
                f"col_{i}", avail_cols,
                index=avail_cols.index(step["column"]) if step["column"] in avail_cols else 0,
                key=f"{sk}_col_{i}", label_visibility="collapsed")
            sel_label = cr[2].text_input(
                f"lab_{i}", value=step["label"],
                key=f"{sk}_lab_{i}", label_visibility="collapsed",
                placeholder="예: 장바구니 담기")
            sel_base = cr[3].selectbox(
                f"base_{i}", ["clicks","previous"],
                index=0 if step["cvr_base"]=="clicks" else 1,
                format_func=lambda x: "클릭 대비" if x=="clicks" else "이전 단계 대비",
                key=f"{sk}_base_{i}", label_visibility="collapsed")
            del_clicked = cr[4].button("🗑️", key=f"{sk}_del_{i}")
            if not del_clicked:
                new_steps.append({"column": sel_col, "label": sel_label, "cvr_base": sel_base})
            else:
                st.session_state[sk] = new_steps + steps[i+1:]
                st.rerun()
        st.session_state[sk] = new_steps
        
        # 단계 추가 / 저장
        bc1, bc2, _ = st.columns([1, 1, 4])
        if bc1.button("➕ 단계 추가", key=f"{sk}_add"):
            st.session_state[sk].append({"column": avail_cols[0], "label": "", "cvr_base": "clicks"})
            st.rerun()
        
        if new_steps and bc2.button("💾 저장", type="primary", key=f"{sk}_save"):
            empty = [i+1 for i, s in enumerate(new_steps) if not s["label"].strip()]
            if empty:
                st.error(f"❌ 라벨이 비어있는 단계: {empty}")
            else:
                save_funnel_steps(adv_code, fpf, new_steps)
                st.success(f"✅ {fpf} 매체 퍼널 {len(new_steps)}단계 저장 완료")
                st.rerun()
        
        # 미리보기
        if new_steps and all(s["label"].strip() for s in new_steps):
            st.divider()
            st.subheader("👀 미리보기 (현재 설정 기준 — 저장 전이라도 즉시 반영)")
            preview_df = pd.read_sql(
                "SELECT * FROM perf WHERE advertiser_code=%s AND platform=%s",
                sqlite3.connect(DB), params=(adv_code, fpf))
            if not preview_df.empty:
                preview_df["date"] = pd.to_datetime(preview_df["date"])
                # order 채워주기
                pv_steps = [{"order":i+1, **s} for i, s in enumerate(new_steps)]
                render_funnel_table(preview_df, pv_steps, group_by="overall", key=f"prev_{fpf}")

# ============ 광고주 관리 ============
# ============ PDF 리포트 다운로드 ============
elif page == "📥 PDF 리포트" and adv_code:
    st.title("📥 PDF 리포트 다운로드")
    st.caption("선택한 기간·매체의 데이터를 PDF로 내보냅니다.")
    
    raw = pd.read_sql("SELECT * FROM perf WHERE advertiser_code=%s", sqlite3.connect(DB), params=(adv_code,))
    if raw.empty:
        st.warning("데이터가 없습니다."); st.stop()
    raw["date"] = pd.to_datetime(raw["date"])
    
    adv_row = q("SELECT name, total_budget, COALESCE(show_conversion,1) FROM advertisers WHERE code=%s", (adv_code,))
    adv_name = adv_row[0][0] if adv_row else adv_code
    total_budget = float(adv_row[0][1] or 0) if adv_row else 0
    show_conv = bool(adv_row[0][2]) if adv_row else True
    
    min_d, max_d = raw["date"].min().date(), raw["date"].max().date()
    
    c1, c2 = st.columns(2)
    with c1:
        d_range = st.date_input("📅 리포트 기간", value=(min_d, max_d),
                                min_value=min_d, max_value=max_d, key="rep_date")
    with c2:
        all_pfs = sorted(raw["platform"].unique())
        sel_pfs = st.multiselect("매체 (전체=비워둠)", all_pfs, default=all_pfs, key="rep_pf")
    
    df_rep = raw.copy()
    if isinstance(d_range, tuple) and len(d_range) == 2:
        df_rep = df_rep[(df_rep["date"] >= pd.Timestamp(d_range[0])) & (df_rep["date"] <= pd.Timestamp(d_range[1]))]
    if sel_pfs:
        df_rep = df_rep[df_rep["platform"].isin(sel_pfs)]
    
    if df_rep.empty:
        st.warning("선택한 조건에 데이터가 없습니다."); st.stop()
    
    mapping = get_conversion_mapping(adv_code)
    df_rep = compute_metrics(df_rep, mapping)
    
    # 미리보기 KPI
    st.subheader("📊 리포트 요약 미리보기")
    pc = st.columns(4)
    pc[0].metric("기간 행 수", f"{len(df_rep):,}")
    pc[1].metric("총 노출", f"{int(df_rep['impressions'].sum()):,}")
    pc[2].metric("총 클릭", f"{int(df_rep['clicks'].sum()):,}")
    pc[3].metric("총 광고비", f"₩{float(df_rep['cost'].sum()):,.0f}")
    
    st.divider()
    st.subheader("📄 PDF 생성")
    st.caption("표지 + 예산 현황 + 핵심 지표 + 일자별 광고비 차트 + 캠페인 TOP 15")
    
    fname_base = f"{adv_code}_report_{datetime.now().strftime('%Y%m%d_%H%M')}"
    
    if st.button("PDF 생성", type="primary", key="gen_pdf"):
        with st.spinner("PDF 생성 중... (차트 렌더링 30초~1분 소요)"):
            try:
                pdf_bytes = build_pdf_report(adv_code, adv_name, df_rep, total_budget, show_conv)
                st.download_button(
                    "⬇️ PDF 다운로드",
                    data=pdf_bytes,
                    file_name=f"{fname_base}.pdf",
                    mime="application/pdf",
                    key="dl_pdf"
                )
                st.success("생성 완료! 위 버튼을 눌러 다운로드하세요.")
            except Exception as e:
                st.error(f"PDF 생성 실패: {e}")
                import traceback; st.code(traceback.format_exc())
elif page == "🏢 광고주 관리":
    st.title("🏢 광고주 관리")
    if not is_admin: st.error("관리자 권한 필요"); st.stop()
    advs = pd.read_sql("""SELECT code AS 코드, name AS 이름,
                          COALESCE(total_budget,0) AS 총예산,
                          COALESCE(show_conversion,1) AS 전환표시,
                          COALESCE(show_creative,0) AS 소재표시,
                          created_at AS 생성일
                          FROM advertisers ORDER BY created_at DESC""", sqlite3.connect(DB))
    st.subheader(f"등록된 광고주 ({len(advs)}개)")
    advs_show = advs.copy()
    advs_show["총예산"] = advs_show["총예산"].apply(lambda x: f"₩{x:,.0f}")
    advs_show["전환표시"] = advs_show["전환표시"].apply(lambda x: "✅ 표시" if x else "❌ 숨김")
    advs_show["소재표시"] = advs_show["소재표시"].apply(lambda x: "✅ 표시" if x else "❌ 숨김")
    st.dataframe(advs_show, use_container_width=True, hide_index=True)
    st.divider()

    st.subheader("➕ 광고주 추가")
    with st.form("add_adv"):
        c1, c2, c3 = st.columns(3)
        with c1: new_code = st.text_input("코드 (예: GAME_C)")
        with c2: new_name = st.text_input("이름")
        with c3: new_budget = st.number_input("총 예산 (₩)", min_value=0, step=100000, value=0)
        cc1, cc2 = st.columns(2)
        with cc1:
            new_show_conv = st.checkbox("전환지표(CPI/CPA 등) 표시", value=True,
                help="끄면 대시보드에서 전환·CPA·CPI 컬럼이 모두 숨겨집니다.")
        with cc2:
            new_show_cre = st.checkbox("광고 소재 분석 탭 표시", value=False,
                help="켜면 '구글_광고소재', '페이스북_광고소재' 탭이 활성화됩니다. 소재 컬럼이 포함된 데이터를 업로드해야 합니다.")
        if st.form_submit_button("추가",type="primary"):
            if not new_code or not new_name: st.error("코드와 이름을 입력하세요")
            else:
                try:
                    adv_code_clean=new_code.strip().upper()
                    q("""INSERT INTO advertisers (code,name,total_budget,show_conversion,show_creative) VALUES (%s,%s,%s,%s,%s)""",(adv_code_clean,new_name.strip(),float(new_budget),1 if new_show_conv else 0,1 if new_show_cre else 0),fetch=False)
                    q("INSERT OR IGNORE INTO permissions VALUES (%s,%s,%s)",(user["email"],adv_code_clean,"OWNER"),fetch=False)
                    email,pw=create_viewer_account(adv_code_clean,new_name)
                    st.success(f"{new_name} 추가 완료\n뷰어 계정: {email}\n비밀번호: {pw}")
                    st.rerun()
                except sqlite3.IntegrityError: st.error("이미 존재하는 코드입니다")
    st.divider()

    st.subheader("✏️ 이름 / 예산 / 표시 옵션 편집")
    if not advs.empty:
        edit_code = st.selectbox("편집할 광고주", advs["코드"].tolist(),
            format_func=lambda c: f"{c} — {advs[advs['코드']==c]['이름'].iloc[0]}")
        cur_row = advs[advs["코드"]==edit_code].iloc[0]
        c1, c2 = st.columns(2)
        with c1: new_name2 = st.text_input("이름", value=cur_row["이름"], key="edit_name")
        with c2: new_budget2 = st.number_input("총 예산 (₩)", min_value=0, step=100000,
                                                value=int(cur_row["총예산"]), key="edit_bud")
        cc1, cc2 = st.columns(2)
        with cc1:
            new_show2 = st.checkbox("전환지표(CPI/CPA 등) 표시",
                value=bool(cur_row["전환표시"]), key="edit_show",
                help="끄면 대시보드에서 전환·CPA·CPI 컬럼이 모두 숨겨집니다.")
        with cc2:
            new_show_cre2 = st.checkbox("광고 소재 분석 탭 표시",
                value=bool(cur_row["소재표시"]), key="edit_show_cre",
                help="켜면 '구글_광고소재', '페이스북_광고소재' 탭이 활성화됩니다.")
        if st.button("변경 저장"):
            q("""UPDATE advertisers SET name=%s, total_budget=%s, show_conversion=%s, show_creative=%s
                 WHERE code=%s""",
              (new_name2, float(new_budget2),
               1 if new_show2 else 0, 1 if new_show_cre2 else 0, edit_code), fetch=False)
            st.success("변경됨"); st.rerun()
    st.divider()

    st.subheader("🗑️ 광고주 삭제 (주의: 데이터·권한·매핑 모두 삭제)")
    if not advs.empty:
        del_code = st.selectbox("삭제할 광고주", advs["코드"].tolist(), key="del_sel",
            format_func=lambda c: f"{c} — {advs[advs['코드']==c]['이름'].iloc[0]}")
        confirm = st.text_input(f"확인을 위해 코드 '{del_code}' 를 입력하세요")
        if st.button("영구 삭제"):
            if confirm == del_code:
                for tbl in ["perf","upload_log","conversion_mapping","permissions"]:
                    q(f"DELETE FROM {tbl} WHERE advertiser_code=%s", (del_code,), fetch=False)
                q("DELETE FROM advertisers WHERE code=%s", (del_code,), fetch=False)
                st.success("삭제됨"); st.rerun()
            else: st.error("코드 불일치")

# ============ 계정 관리 ============
elif page == "👤 계정 관리":
    st.title("👤 계정 관리")
    if not is_admin:
        st.error("관리자만 접근 가능합니다"); st.stop()

    # ===== 계정 목록 =====
    users_df = pd.read_sql("""
    SELECT u.email,u.name,u.role,
           GROUP_CONCAT(p.advertiser_code) AS advertisers
    FROM users u
    LEFT JOIN permissions p ON u.email=p.email
    GROUP BY u.email
    ORDER BY u.email
    """, sqlite3.connect(DB))

    st.subheader("📋 계정 목록")
    st.dataframe(users_df, use_container_width=True, hide_index=True)

    st.divider()

    # ===== 계정 생성 =====
    st.subheader("➕ 계정 생성")

    new_email = st.text_input("이메일")
    new_name = st.text_input("이름")
    new_pw = st.text_input("비밀번호", type="password")
    new_role = st.selectbox("권한", ["AGENCY_ADMIN","OWNER","MANAGER","VIEWER"], key="new_role")

    adv_list = q("SELECT code FROM advertisers", fetch=True)
    adv_options = [a[0] for a in adv_list]
    sel_advs = st.multiselect("광고주 연결", adv_options)

    if st.button("계정 생성"):
        if not new_email or not new_pw:
            st.error("이메일/비밀번호 입력 필요")
        else:
            try:
                q("INSERT INTO users VALUES (%s,%s,%s,%s)",
                  (new_email,new_name,new_role,new_pw), fetch=False)

                for adv in sel_advs:
                    q("INSERT INTO permissions VALUES (%s,%s,%s)",
                      (new_email,adv,new_role), fetch=False)

                st.success("계정 생성 완료"); st.rerun()
            except:
                st.error("이미 존재하는 계정")

    st.divider()

    # ===== 계정 수정 =====
    st.subheader("✏️ 계정 수정")

    sel_user = st.selectbox("계정 선택", users_df["email"], key="select_user")

    urow = users_df[users_df["email"]==sel_user].iloc[0]

    edit_name = st.text_input("이름", value=urow["name"])
    roles = ["AGENCY_ADMIN","OWNER","MANAGER","VIEWER"]
    edit_role = st.selectbox(
        "권한", 
        roles,
        index=roles.index(urow["role"]) if urow["role"] in roles else 0,
        key="edit_role_select")
    new_pw2 = st.text_input("새 비밀번호", type="password")

    adv_list = q("SELECT code FROM advertisers", fetch=True)
    adv_options = [a[0] for a in adv_list]

    cur_advs = urow["advertisers"].split(",") if urow["advertisers"] else []
    edit_advs = st.multiselect("광고주", adv_options, default=cur_advs)

    if st.button("수정 저장"):
        q("UPDATE users SET name=%s,role=%s WHERE email=%s",
          (edit_name,edit_role,sel_user), fetch=False)

        if new_pw2:
            q("UPDATE users SET password=%s WHERE email=%s",
              (new_pw2,sel_user), fetch=False)

        q("DELETE FROM permissions WHERE email=%s", (sel_user,), fetch=False)

        for adv in edit_advs:
            q("INSERT INTO permissions VALUES (%s,%s,%s)",
              (sel_user,adv,edit_role), fetch=False)

        st.success("수정 완료"); st.rerun()

    st.divider()

    # ===== 계정 삭제 =====
    st.subheader("🗑️ 계정 삭제")

    del_user = st.selectbox("삭제할 계정", users_df["email"], key="del_user")

    if st.button("삭제"):
        q("DELETE FROM permissions WHERE email=%s", (del_user,), fetch=False)
        q("DELETE FROM users WHERE email=%s", (del_user,), fetch=False)
        st.success("삭제 완료"); st.rerun()
