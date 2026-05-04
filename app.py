# app.py — AdHub v3
import streamlit as st
import pandas as pd
import sqlite3, json
from datetime import datetime
import plotly.express as px
import plotly.graph_objects as go

st.set_page_config(page_title="AdHub", page_icon="📊", layout="wide")
DB = "adhub.db"

# ============ DB ============
def init_db():
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, name TEXT, role TEXT, password TEXT);
    CREATE TABLE IF NOT EXISTS advertisers (
        code TEXT PRIMARY KEY, name TEXT,
        total_budget REAL DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS permissions (
        email TEXT, advertiser_code TEXT, level TEXT,
        PRIMARY KEY (email, advertiser_code));
    CREATE TABLE IF NOT EXISTS perf (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        advertiser_code TEXT, platform TEXT, date TEXT,
        campaign TEXT, adgroup TEXT,
        impressions INTEGER, clicks INTEGER, cost REAL, raw_data TEXT);
    CREATE TABLE IF NOT EXISTS upload_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT, advertiser_code TEXT, platform TEXT,
        file_name TEXT, rows INTEGER, uploaded_at TEXT);
    CREATE TABLE IF NOT EXISTS conversion_mapping (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        advertiser_code TEXT, platform TEXT, campaign TEXT,
        conversion_column TEXT, conversion_label TEXT, updated_at TEXT,
        UNIQUE(advertiser_code, platform, campaign));
    """)
    cur.executemany("INSERT OR IGNORE INTO users VALUES (?,?,?,?)", [
        ("admin@adhub.com",  "김에이전시", "AGENCY_ADMIN", "1234"),
        ("manager@scon.com", "박마케터",   "MANAGER",      "1234"),
        ("viewer@scon.com",  "최뷰어",     "VIEWER",       "1234"),
    ])
    cur.executemany("INSERT OR IGNORE INTO advertisers (code,name) VALUES (?,?)", [
        ("SCONEC", "스코넥엔터테인먼트"), ("GAME_A", "게임사 A"), ("GAME_B", "게임사 B"),
    ])
    cur.executemany("INSERT OR IGNORE INTO permissions VALUES (?,?,?)", [
        ("admin@adhub.com",  "SCONEC", "OWNER"),
        ("admin@adhub.com",  "GAME_A", "OWNER"),
        ("admin@adhub.com",  "GAME_B", "OWNER"),
        ("manager@scon.com", "SCONEC", "EDITOR"),
        ("viewer@scon.com",  "SCONEC", "VIEWER"),
    ])
    con.commit(); con.close()

def migrate_db():
    con = sqlite3.connect(DB); cur = con.cursor()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(advertisers)").fetchall()]
    if "created_at" not in cols:
        cur.execute("ALTER TABLE advertisers ADD COLUMN created_at TEXT")
        cur.execute("UPDATE advertisers SET created_at = datetime('now') WHERE created_at IS NULL")
    if "total_budget" not in cols:
        cur.execute("ALTER TABLE advertisers ADD COLUMN total_budget REAL DEFAULT 0")
    if "show_conversion" not in cols:
        cur.execute("ALTER TABLE advertisers ADD COLUMN show_conversion INTEGER DEFAULT 1")
    cols = [r[1] for r in cur.execute("PRAGMA table_info(perf)").fetchall()]
    if "raw_data" not in cols:
        cur.execute("ALTER TABLE perf ADD COLUMN raw_data TEXT")
    con.commit(); con.close()

init_db(); migrate_db()

def q(sql, params=(), fetch=True):
    con = sqlite3.connect(DB); cur = con.cursor(); cur.execute(sql, params)
    rows = cur.fetchall() if fetch else None
    con.commit(); con.close(); return rows

def safe_div(a, b): return (a/b) if b else 0

# ============ 로그인 ============
def login_view():
    st.title("📊 AdHub 로그인")
    st.caption("데모 계정: admin@adhub.com / manager@scon.com / viewer@scon.com (비번: 1234)")
    email = st.text_input("이메일"); pw = st.text_input("비밀번호", type="password")
    if st.button("로그인", type="primary"):
        row = q("SELECT email,name,role FROM users WHERE email=? AND password=?", (email, pw))
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
    JOIN advertisers a ON a.code=p.advertiser_code WHERE p.email=? ORDER BY a.name""", (user["email"],))

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
    page = st.radio("메뉴", menu)

# ============ 컬럼 매퍼 ============
GOOGLE_CORE = {"일":"date","캠페인":"campaign","광고그룹":"adgroup","노출수":"impressions","클릭수":"clicks","비용":"cost"}
GOOGLE_CONV_CANDIDATES = ["전환수","설치","사전예약"]
FB_CORE = {"보고 시작":"date","캠페인 이름":"campaign","광고 세트 이름":"adgroup","노출":"impressions","클릭(전체)":"clicks","지출 금액 (KRW)":"cost","지출 금액 (USD)":"cost_usd"}
FB_CONV_CANDIDATES = ["결과","페이지 참여","링크 클릭","게시물 공감","게시물 댓글","팔로우 또는 좋아요"]

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
    rows = q("SELECT platform, campaign, conversion_column, conversion_label FROM conversion_mapping WHERE advertiser_code=?", (adv_code,))
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
    for path in ["/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
                 "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                 "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]:
        try:
            pdfmetrics.registerFont(TTFont("Korean", path))
            font_name = "Korean"
            break
        except: continue
    
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
        "지표 선택", ["광고비 (₩)","CTR (%)","CPM (₩)","CPC (₩)",f"{conv_label} (₩)"],
        horizontal=True, key=f"{key_prefix}_metric")
    daily = df.groupby(["date","platform"], as_index=False).agg(
        impressions=("impressions","sum"), clicks=("clicks","sum"),
        cost=("cost","sum"), conversions=("conversions","sum"))
    daily["CTR"] = daily.apply(lambda r: safe_div(r.clicks, r.impressions)*100, axis=1)
    daily["CPM"] = daily.apply(lambda r: safe_div(r.cost, r.impressions)*1000, axis=1)
    daily["CPC"] = daily.apply(lambda r: safe_div(r.cost, r.clicks), axis=1)
    daily["CPA"] = daily.apply(lambda r: safe_div(r.cost, r.conversions), axis=1)
    mmap = {"광고비 (₩)":"cost","CTR (%)":"CTR","CPM (₩)":"CPM","CPC (₩)":"CPC",f"{conv_label} (₩)":"CPA"}
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
    st.dataframe(show, use_container_width=True, hide_index=True)

# ============ 대시보드 ============
if page == "📈 대시보드" and adv_code:
    st.title(f"📈 {sel_name} — 성과 대시보드")
    raw = pd.read_sql("SELECT * FROM perf WHERE advertiser_code=?", sqlite3.connect(DB), params=(adv_code,))
    if raw.empty:
        st.warning("데이터가 없습니다. '데이터 업로드' 메뉴에서 파일을 올려주세요."); st.stop()
    raw["date"] = pd.to_datetime(raw["date"])
    min_d, max_d = raw["date"].min().date(), raw["date"].max().date()

    adv_row = q("SELECT total_budget, COALESCE(show_conversion,1) FROM advertisers WHERE code=?", (adv_code,))
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
    tab_labels = ["📊 Summary"]
    tab_keys = ["summary"]
    if "GOOGLE" in available:
        tab_labels.append("🟦 Google"); tab_keys.append("google")
    if "FACEBOOK" in available:
        tab_labels.append("🟪 Facebook"); tab_keys.append("facebook")
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
                    
# ============ 데이터 업로드 ============
elif page == "📤 데이터 업로드" and adv_code:
    st.title("📤 로우데이터 업로드")
    if my_level == "VIEWER":
        st.error("⛔ VIEWER 권한은 업로드할 수 없습니다."); st.stop()
    platform = st.radio("매체 선택", ["GOOGLE","FACEBOOK"], horizontal=True)
    file = st.file_uploader("파일 업로드 (xlsx / csv)", type=["xlsx","xls","csv"])
    if file:
        try:
            df, conv_present = parse_file(file, platform)
            st.success(f"✅ 파싱 성공: {len(df)}행")
            if conv_present:
                st.info(f"감지된 전환 후보 컬럼: **{', '.join(conv_present)}** → '🎯 전환지표 설정'에서 매핑하세요.")
            st.dataframe(df.head(8), use_container_width=True, hide_index=True)
            if st.button("🚀 DB에 저장", type="primary"):
                con = sqlite3.connect(DB); cur = con.cursor()
                for _, r in df.iterrows():
                    cur.execute("""INSERT INTO perf (advertiser_code,platform,date,campaign,adgroup,
                        impressions,clicks,cost,raw_data) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (adv_code, platform, r["date"], r["campaign"], r["adgroup"],
                         int(r["impressions"] or 0), int(r["clicks"] or 0),
                         float(r["cost"] or 0), r["raw_data"]))
                cur.execute("""INSERT INTO upload_log (email,advertiser_code,platform,file_name,rows,uploaded_at)
                    VALUES (?,?,?,?,?,?)""", (user["email"], adv_code, platform, file.name, len(df),
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                con.commit(); con.close()
                st.success(f"🎉 {len(df)}행 저장 완료!"); st.balloons()
        except Exception as e:
            st.error(f"파싱 오류: {e}")
            import traceback; st.code(traceback.format_exc())

# ============ 업로드 이력 ============
elif page == "📋 업로드 이력" and adv_code:
    st.title("📋 업로드 이력")
    logs = pd.read_sql("""SELECT uploaded_at AS 업로드시각, email AS 사용자,
                          platform AS 매체, file_name AS 파일명, rows AS 행수
                          FROM upload_log WHERE advertiser_code=? ORDER BY id DESC""",
                       sqlite3.connect(DB), params=(adv_code,))
    st.dataframe(logs, use_container_width=True, hide_index=True)

# ============ 전환지표 설정 ============
elif page == "🎯 전환지표 설정" and adv_code:
    st.title("🎯 전환지표 매핑 설정")
    st.caption("캠페인 성격에 따라 어떤 컬럼을 '전환수'로 쓸지, 어떤 라벨(CPI/CPA)로 표시할지 지정합니다.")
    cur_map = pd.read_sql("""SELECT platform AS 매체, campaign AS 캠페인,
                             conversion_column AS 전환컬럼, conversion_label AS 라벨,
                             updated_at AS 수정시각 FROM conversion_mapping
                             WHERE advertiser_code=? ORDER BY platform, campaign""",
                          sqlite3.connect(DB), params=(adv_code,))
    st.subheader("📌 현재 매핑")
    if cur_map.empty: st.info("아직 매핑이 없습니다.")
    else: st.dataframe(cur_map, use_container_width=True, hide_index=True)
    st.divider()
    if my_level in ("OWNER","EDITOR") or is_admin:
        st.subheader("➕ 매핑 추가/수정")
        raw = pd.read_sql("SELECT platform, campaign, raw_data FROM perf WHERE advertiser_code=?",
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
                     VALUES (?,?,?,?,?,?)
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
                q("DELETE FROM conversion_mapping WHERE advertiser_code=? AND platform=? AND campaign=?",
                  (adv_code, row["매체"], row["캠페인"]), fetch=False)
                st.success("삭제됨"); st.rerun()

# ============ 광고주 관리 ============
# ============ PDF 리포트 다운로드 ============
elif page == "📥 PDF 리포트" and adv_code:
    st.title("📥 PDF 리포트 다운로드")
    st.caption("선택한 기간·매체의 데이터를 PDF로 내보냅니다.")
    
    raw = pd.read_sql("SELECT * FROM perf WHERE advertiser_code=?", sqlite3.connect(DB), params=(adv_code,))
    if raw.empty:
        st.warning("데이터가 없습니다."); st.stop()
    raw["date"] = pd.to_datetime(raw["date"])
    
    adv_row = q("SELECT name, total_budget, COALESCE(show_conversion,1) FROM advertisers WHERE code=?", (adv_code,))
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
                          created_at AS 생성일
                          FROM advertisers ORDER BY created_at DESC""", sqlite3.connect(DB))
    st.subheader(f"등록된 광고주 ({len(advs)}개)")
    advs_show = advs.copy()
    advs_show["총예산"] = advs_show["총예산"].apply(lambda x: f"₩{x:,.0f}")
    advs_show["전환표시"] = advs_show["전환표시"].apply(lambda x: "✅ 표시" if x else "❌ 숨김")
    st.dataframe(advs_show, use_container_width=True, hide_index=True)
    st.divider()

    st.subheader("➕ 광고주 추가")
    with st.form("add_adv"):
        c1, c2, c3 = st.columns(3)
        with c1: new_code = st.text_input("코드 (예: GAME_C)")
        with c2: new_name = st.text_input("이름")
        with c3: new_budget = st.number_input("총 예산 (₩)", min_value=0, step=100000, value=0)
        new_show_conv = st.checkbox("전환지표(CPI/CPA 등) 표시", value=True,
            help="끄면 대시보드에서 전환·CPA·CPI 컬럼이 모두 숨겨집니다.")
        if st.form_submit_button("추가", type="primary"):
            if not new_code or not new_name: st.error("코드와 이름을 입력하세요")
            else:
                try:
                    q("INSERT INTO advertisers (code,name,total_budget,show_conversion) VALUES (?,?,?,?)",
                      (new_code.strip().upper(), new_name.strip(), float(new_budget),
                       1 if new_show_conv else 0), fetch=False)
                    q("INSERT OR IGNORE INTO permissions VALUES (?,?,?)",
                      (user["email"], new_code.strip().upper(), "OWNER"), fetch=False)
                    st.success(f"'{new_name}' 추가 완료"); st.rerun()
                except sqlite3.IntegrityError: st.error("이미 존재하는 코드입니다")
    st.divider()

    st.subheader("✏️ 이름 / 예산 / 전환지표 표시 편집")
    if not advs.empty:
        edit_code = st.selectbox("편집할 광고주", advs["코드"].tolist(),
            format_func=lambda c: f"{c} — {advs[advs['코드']==c]['이름'].iloc[0]}")
        cur_row = advs[advs["코드"]==edit_code].iloc[0]
        c1, c2 = st.columns(2)
        with c1: new_name2 = st.text_input("이름", value=cur_row["이름"], key="edit_name")
        with c2: new_budget2 = st.number_input("총 예산 (₩)", min_value=0, step=100000,
                                                value=int(cur_row["총예산"]), key="edit_bud")
        new_show2 = st.checkbox("전환지표(CPI/CPA 등) 표시",
            value=bool(cur_row["전환표시"]), key="edit_show",
            help="끄면 대시보드에서 전환·CPA·CPI 컬럼이 모두 숨겨집니다.")
        if st.button("변경 저장"):
            q("UPDATE advertisers SET name=?, total_budget=?, show_conversion=? WHERE code=?",
              (new_name2, float(new_budget2), 1 if new_show2 else 0, edit_code), fetch=False)
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
                    q(f"DELETE FROM {tbl} WHERE advertiser_code=?", (del_code,), fetch=False)
                q("DELETE FROM advertisers WHERE code=?", (del_code,), fetch=False)
                st.success("삭제됨"); st.rerun()
            else: st.error("코드 불일치")
