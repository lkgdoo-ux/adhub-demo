# app.py — AdHub v2: 광고 리포팅 + 어드민 (단일 파일)
import streamlit as st
import pandas as pd
import sqlite3, json
from datetime import datetime, date, timedelta
import plotly.express as px
import plotly.graph_objects as go

st.set_page_config(page_title="AdHub", page_icon="📊", layout="wide")

DB = "adhub.db"

# ============================================================
# 1) DB 초기화 + 마이그레이션
# ============================================================
def init_db():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        email TEXT PRIMARY KEY, name TEXT, role TEXT, password TEXT
    );
    CREATE TABLE IF NOT EXISTS advertisers (
        code TEXT PRIMARY KEY, name TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS permissions (
        email TEXT, advertiser_code TEXT, level TEXT,
        PRIMARY KEY (email, advertiser_code)
    );
    CREATE TABLE IF NOT EXISTS perf (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        advertiser_code TEXT, platform TEXT, date TEXT,
        campaign TEXT, adgroup TEXT,
        impressions INTEGER, clicks INTEGER, cost REAL,
        raw_data TEXT  -- JSON: 전환 후보 컬럼들 모두 보관
    );
    CREATE TABLE IF NOT EXISTS upload_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT, advertiser_code TEXT, platform TEXT,
        file_name TEXT, rows INTEGER, uploaded_at TEXT
    );
    CREATE TABLE IF NOT EXISTS conversion_mapping (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        advertiser_code TEXT, platform TEXT,
        campaign TEXT,             -- '*' = 광고주+매체 전체 기본값
        conversion_column TEXT,    -- raw_data 의 키 (예: '설치', '페이지 참여', '결과')
        conversion_label TEXT,     -- 표시 라벨 (예: 'CPI', 'CPA')
        updated_at TEXT,
        UNIQUE(advertiser_code, platform, campaign)
    );
    """)
    cur.executemany("INSERT OR IGNORE INTO users VALUES (?,?,?,?)", [
        ("admin@adhub.com",  "김에이전시", "AGENCY_ADMIN", "1234"),
        ("manager@scon.com", "박마케터",   "MANAGER",      "1234"),
        ("viewer@scon.com",  "최뷰어",     "VIEWER",       "1234"),
    ])
    cur.executemany("INSERT OR IGNORE INTO advertisers (code,name) VALUES (?,?)", [
        ("SCONEC", "스코넥엔터테인먼트"),
        ("GAME_A", "게임사 A"),
        ("GAME_B", "게임사 B"),
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
    """기존 DB에 누락된 컬럼을 안전하게 추가"""
    con = sqlite3.connect(DB)
    cur = con.cursor()
    
    # advertisers 테이블에 created_at 컬럼 추가
    cols = [r[1] for r in cur.execute("PRAGMA table_info(advertisers)").fetchall()]
    if "created_at" not in cols:
        cur.execute("ALTER TABLE advertisers ADD COLUMN created_at TEXT")
        cur.execute("UPDATE advertisers SET created_at = datetime('now') WHERE created_at IS NULL")
    
    # perf 테이블에 raw_data 컬럼 추가 (v1→v2 호환)
    cols = [r[1] for r in cur.execute("PRAGMA table_info(perf)").fetchall()]
    if "raw_data" not in cols:
        cur.execute("ALTER TABLE perf ADD COLUMN raw_data TEXT")
    
    con.commit(); con.close()

init_db()
migrate_db()

def q(sql, params=(), fetch=True):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall() if fetch else None
    con.commit(); con.close()
    return rows

# ============================================================
# 2) 로그인
# ============================================================
def login_view():
    st.title("📊 AdHub 로그인")
    st.caption("데모 계정: admin@adhub.com / manager@scon.com / viewer@scon.com (비번: 1234)")
    email = st.text_input("이메일")
    pw = st.text_input("비밀번호", type="password")
    if st.button("로그인", type="primary"):
        row = q("SELECT email,name,role FROM users WHERE email=? AND password=?", (email, pw))
        if row:
            r = row[0]
            st.session_state.user = {"email": r[0], "name": r[1], "role": r[2]}
            st.rerun()
        else:
            st.error("로그인 실패")

if "user" not in st.session_state:
    login_view(); st.stop()

user = st.session_state.user
is_admin = user["role"] in ("AGENCY_ADMIN", "SUPER_ADMIN")

# ============================================================
# 3) 사이드바
# ============================================================
my_advs = q("""
    SELECT a.code, a.name, p.level FROM permissions p 
    JOIN advertisers a ON a.code=p.advertiser_code WHERE p.email=?
    ORDER BY a.name""", (user["email"],))

with st.sidebar:
    st.markdown(f"**👤 {user['name']}**  \n`{user['role']}`")
    if st.button("로그아웃"): del st.session_state.user; st.rerun()
    st.divider()
    
    if not my_advs:
        st.warning("접근 가능한 광고주 없음")
        adv_code, my_level, sel_name = None, None, None
    else:
        adv_options = {f"{name} ({code})": (code, level) for code, name, level in my_advs}
        sel = st.selectbox("광고주 선택", list(adv_options.keys()))
        adv_code, my_level = adv_options[sel]
        sel_name = sel
        st.info(f"권한: **{my_level}**")
    
  # 권한별 메뉴 구성
    menu = ["📈 대시보드"]
    if my_level in ("OWNER", "EDITOR") or is_admin:
        menu += ["📤 데이터 업로드", "📋 업로드 이력", "🎯 전환지표 설정"]
    if is_admin:
        menu.append("🏢 광고주 관리")
    page = st.radio("메뉴", menu)

# ============================================================
# 4) 컬럼 매퍼 (Google / Facebook)
# ============================================================
GOOGLE_CORE = {
    "일":"date", "캠페인":"campaign", "광고그룹":"adgroup",
    "노출수":"impressions", "클릭수":"clicks", "비용":"cost",
}
GOOGLE_CONV_CANDIDATES = ["전환수","설치","사전예약"]

FB_CORE = {
    "보고 시작":"date", "캠페인 이름":"campaign", "광고 세트 이름":"adgroup",
    "노출":"impressions", "클릭(전체)":"clicks",
    "지출 금액 (KRW)":"cost", "지출 금액 (USD)":"cost_usd",
}
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
    
    if "adgroup" not in df.columns:
        df["adgroup"] = df.get("campaign", "")
    
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

# ============================================================
# 5) 전환 매핑 헬퍼
# ============================================================
def get_conversion_mapping(adv_code):
    rows = q("""SELECT platform, campaign, conversion_column, conversion_label
                FROM conversion_mapping WHERE advertiser_code=?""", (adv_code,))
    return {(p, c): (col, lbl) for p, c, col, lbl in rows}

def resolve_conv(mapping, platform, campaign):
    if (platform, campaign) in mapping: return mapping[(platform, campaign)]
    if (platform, "*") in mapping: return mapping[(platform, "*")]
    return (None, "CPA")

def compute_metrics(df, mapping):
    if df.empty:
        df["conversions"] = 0; df["conv_label"] = "CPA"; df["conv_column"] = ""
        return df
    
    def get_conv(row):
        col, _ = resolve_conv(mapping, row["platform"], row["campaign"])
        if not col: return 0
        try:
            d = json.loads(row["raw_data"]) if row["raw_data"] else {}
            return float(d.get(col, 0))
        except: return 0
    def get_label(row):
        _, lbl = resolve_conv(mapping, row["platform"], row["campaign"])
        return lbl
    def get_col(row):
        col, _ = resolve_conv(mapping, row["platform"], row["campaign"])
        return col or ""
    
    df = df.copy()
    df["conversions"] = df.apply(get_conv, axis=1)
    df["conv_label"] = df.apply(get_label, axis=1)
    df["conv_column"] = df.apply(get_col, axis=1)
    return df

def safe_div(a, b):
    return (a / b) if b else 0

# ============================================================
# 6) 페이지: 대시보드
# ============================================================
if page == "📈 대시보드" and adv_code:
    st.title(f"📈 {sel_name} — 성과 대시보드")
    
    raw = pd.read_sql(
        "SELECT * FROM perf WHERE advertiser_code=?",
        sqlite3.connect(DB), params=(adv_code,))
    
    if raw.empty:
        st.warning("데이터가 없습니다. '데이터 업로드' 메뉴에서 파일을 올려주세요.")
        st.stop()
    
    raw["date"] = pd.to_datetime(raw["date"])
    min_d, max_d = raw["date"].min().date(), raw["date"].max().date()
    
    fc1, fc2, fc3 = st.columns([2,1,1])
    with fc1:
        date_range = st.date_input(
            "📅 기간 선택", value=(min_d, max_d),
            min_value=min_d, max_value=max_d)
    with fc2:
        platforms = sorted(raw["platform"].unique())
        sel_platforms = st.multiselect("매체", platforms, default=platforms)
    with fc3:
        campaigns = sorted(raw["campaign"].dropna().unique())
        sel_camps = st.multiselect("캠페인 (전체=비워둠)", campaigns)
    
    df = raw.copy()
    if isinstance(date_range, tuple) and len(date_range) == 2:
        d_from, d_to = date_range
        df = df[(df["date"] >= pd.Timestamp(d_from)) & (df["date"] <= pd.Timestamp(d_to))]
    if sel_platforms:
        df = df[df["platform"].isin(sel_platforms)]
    if sel_camps:
        df = df[df["campaign"].isin(sel_camps)]
    
    if df.empty:
        st.warning("선택한 조건에 해당하는 데이터가 없습니다."); st.stop()
    
    mapping = get_conversion_mapping(adv_code)
    df = compute_metrics(df, mapping)
    
    tot_imp = int(df["impressions"].sum())
    tot_clk = int(df["clicks"].sum())
    tot_cost = float(df["cost"].sum())
    tot_conv = float(df["conversions"].sum())
    
    ctr = safe_div(tot_clk, tot_imp) * 100
    cpm = safe_div(tot_cost, tot_imp) * 1000
    cpc = safe_div(tot_cost, tot_clk)
    cpa = safe_div(tot_cost, tot_conv)
    
    labels = sorted(set(df["conv_label"].dropna().unique()))
    conv_label = "/".join(labels) if labels else "CPA"
    
    k1,k2,k3,k4 = st.columns(4)
    k1.metric("노출 (Impression)", f"{tot_imp:,}")
    k2.metric("클릭 (Click)", f"{tot_clk:,}")
    k3.metric("광고비 (Cost)", f"₩{tot_cost:,.0f}")
    k4.metric(f"전환 ({conv_label})", f"{tot_conv:,.0f}")
    
    k5,k6,k7,k8 = st.columns(4)
    k5.metric("CTR", f"{ctr:.2f}%")
    k6.metric("CPM", f"₩{cpm:,.0f}")
    k7.metric("CPC", f"₩{cpc:,.0f}")
    k8.metric(conv_label, f"₩{cpa:,.0f}" if tot_conv else "—")
    
    st.divider()
    
    st.subheader("📊 일자별 효율 지표")
    metric_choice = st.radio(
        "지표 선택", ["CTR (%)", "CPM (₩)", "CPC (₩)", f"{conv_label} (₩)", "광고비 (₩)"],
        horizontal=True)
    
    daily = df.groupby(["date","platform"], as_index=False).agg(
        impressions=("impressions","sum"),
        clicks=("clicks","sum"),
        cost=("cost","sum"),
        conversions=("conversions","sum"),
    )
    daily["CTR"] = daily.apply(lambda r: safe_div(r.clicks, r.impressions)*100, axis=1)
    daily["CPM"] = daily.apply(lambda r: safe_div(r.cost, r.impressions)*1000, axis=1)
    daily["CPC"] = daily.apply(lambda r: safe_div(r.cost, r.clicks), axis=1)
    daily["CPA"] = daily.apply(lambda r: safe_div(r.cost, r.conversions), axis=1)
    
    metric_map = {"CTR (%)":"CTR", "CPM (₩)":"CPM", "CPC (₩)":"CPC",
                  f"{conv_label} (₩)":"CPA", "광고비 (₩)":"cost"}
    y_col = metric_map[metric_choice]
    
    fig = px.line(daily, x="date", y=y_col, color="platform",
                  markers=True, title=f"일자별 {metric_choice} 추이")
    fig.update_layout(height=400)
    st.plotly_chart(fig, use_container_width=True)
    
    st.subheader("🎯 캠페인별 효율")
    by_camp = df.groupby(["platform","campaign"], as_index=False).agg(
        impressions=("impressions","sum"),
        clicks=("clicks","sum"),
        cost=("cost","sum"),
        conversions=("conversions","sum"),
    )
    by_camp["CTR (%)"] = by_camp.apply(lambda r: round(safe_div(r.clicks,r.impressions)*100,2), axis=1)
    by_camp["CPM (₩)"] = by_camp.apply(lambda r: round(safe_div(r.cost,r.impressions)*1000), axis=1)
    by_camp["CPC (₩)"] = by_camp.apply(lambda r: round(safe_div(r.cost,r.clicks)), axis=1)
    by_camp["CPA/CPI (₩)"] = by_camp.apply(lambda r: round(safe_div(r.cost,r.conversions)) if r.conversions else 0, axis=1)
    by_camp["cost"] = by_camp["cost"].astype(int)
    by_camp = by_camp.rename(columns={"platform":"매체","campaign":"캠페인",
        "impressions":"노출","clicks":"클릭","cost":"광고비","conversions":"전환"})
    st.dataframe(by_camp, use_container_width=True, hide_index=True)
    
    st.subheader("📋 상세 데이터")
    show = df.copy()
    show["CTR (%)"] = show.apply(lambda r: round(safe_div(r.clicks,r.impressions)*100,2), axis=1)
    show["CPC (₩)"] = show.apply(lambda r: round(safe_div(r.cost,r.clicks)), axis=1)
    show["CPA/CPI (₩)"] = show.apply(lambda r: round(safe_div(r.cost,r.conversions)) if r.conversions else 0, axis=1)
    
    drop_cols = ["id","advertiser_code","platform","raw_data","conv_column"]
    show = show.drop(columns=[c for c in drop_cols if c in show.columns])
    show["date"] = pd.to_datetime(show["date"]).dt.strftime("%Y-%m-%d")
    show = show.rename(columns={
        "date":"일자","campaign":"캠페인","adgroup":"광고그룹",
        "impressions":"노출","clicks":"클릭","cost":"광고비",
        "conversions":"전환","conv_label":"전환라벨"})
    st.dataframe(show, use_container_width=True, hide_index=True, height=400)

# ============================================================
# 7) 페이지: 데이터 업로드
# ============================================================
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
                st.info(f"감지된 전환 후보 컬럼: **{', '.join(conv_present)}** "
                        "→ '🎯 전환지표 설정'에서 캠페인별로 매핑하세요.")
            st.dataframe(df.head(8), use_container_width=True, hide_index=True)
            
            if st.button("🚀 DB에 저장", type="primary"):
                con = sqlite3.connect(DB)
                cur = con.cursor()
                for _, r in df.iterrows():
                    cur.execute("""INSERT INTO perf
                        (advertiser_code,platform,date,campaign,adgroup,
                         impressions,clicks,cost,raw_data)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                        (adv_code, platform, r["date"], r["campaign"], r["adgroup"],
                         int(r["impressions"] or 0), int(r["clicks"] or 0),
                         float(r["cost"] or 0), r["raw_data"]))
                cur.execute("""INSERT INTO upload_log
                    (email,advertiser_code,platform,file_name,rows,uploaded_at)
                    VALUES (?,?,?,?,?,?)""",
                    (user["email"], adv_code, platform, file.name, len(df),
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                con.commit(); con.close()
                st.success(f"🎉 {len(df)}행이 저장되었습니다!"); st.balloons()
        except Exception as e:
            st.error(f"파싱 오류: {e}")
            import traceback; st.code(traceback.format_exc())

# ============================================================
# 8) 페이지: 업로드 이력
# ============================================================
elif page == "📋 업로드 이력" and adv_code:
    st.title("📋 업로드 이력")
    logs = pd.read_sql("""SELECT uploaded_at AS 업로드시각, email AS 사용자,
                          platform AS 매체, file_name AS 파일명, rows AS 행수
                          FROM upload_log WHERE advertiser_code=?
                          ORDER BY id DESC""",
                       sqlite3.connect(DB), params=(adv_code,))
    st.dataframe(logs, use_container_width=True, hide_index=True)

# ============================================================
# 9) 페이지: 🎯 전환지표 설정
# ============================================================
elif page == "🎯 전환지표 설정" and adv_code:
    st.title("🎯 전환지표 매핑 설정")
    st.caption("캠페인 성격에 따라 어떤 컬럼을 '전환수'로 쓸지, 어떤 라벨(CPI/CPA)로 표시할지 지정합니다.")
    
    if my_level == "VIEWER":
        st.warning("VIEWER 권한은 조회만 가능합니다.")
    
    cur_map = pd.read_sql("""SELECT platform AS 매체, campaign AS 캠페인,
                             conversion_column AS 전환컬럼, conversion_label AS 라벨,
                             updated_at AS 수정시각
                             FROM conversion_mapping WHERE advertiser_code=?
                             ORDER BY platform, campaign""",
                          sqlite3.connect(DB), params=(adv_code,))
    
    st.subheader("📌 현재 매핑")
    if cur_map.empty:
        st.info("아직 매핑이 없습니다. 매체별 기본값과 캠페인별 매핑을 추가해주세요.")
    else:
        st.dataframe(cur_map, use_container_width=True, hide_index=True)
    
    st.divider()
    
    if my_level in ("OWNER","EDITOR") or is_admin:
        st.subheader("➕ 매핑 추가/수정")
        
        raw = pd.read_sql("SELECT platform, campaign, raw_data FROM perf WHERE advertiser_code=?",
                          sqlite3.connect(DB), params=(adv_code,))
        
        col1, col2 = st.columns(2)
        with col1:
            sel_pf = st.selectbox("매체", ["GOOGLE","FACEBOOK"])
        with col2:
            camps = ["* (이 매체의 기본값)"] + sorted(
                raw[raw["platform"]==sel_pf]["campaign"].dropna().unique().tolist())
            sel_camp = st.selectbox("캠페인", camps)
            sel_camp_val = "*" if sel_camp.startswith("*") else sel_camp
        
        conv_keys = set()
        sub = raw[raw["platform"]==sel_pf]
        for rd in sub["raw_data"].dropna():
            try: conv_keys.update(json.loads(rd).keys())
            except: pass
        conv_keys = sorted(conv_keys)
        
        if not conv_keys:
            st.warning(f"{sel_pf} 데이터가 없거나 전환 후보 컬럼이 없습니다. 먼저 데이터를 업로드하세요.")
        else:
            col3, col4 = st.columns(2)
            with col3:
                sel_col = st.selectbox("전환으로 사용할 컬럼", conv_keys,
                    help="이 컬럼의 값을 '전환수'로 집계해 CPA/CPI를 계산합니다.")
            with col4:
                sel_lbl = st.selectbox("표시 라벨", ["CPI","CPA","CPL","CPV","CPE"],
                    help="대시보드에서 이 매핑이 적용된 캠페인은 이 라벨로 표시됩니다.")
            
            if st.button("💾 저장", type="primary"):
                q("""INSERT INTO conversion_mapping
                     (advertiser_code,platform,campaign,conversion_column,conversion_label,updated_at)
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
            del_idx = st.selectbox("삭제할 매핑 선택",
                cur_map.index,
                format_func=lambda i: f"{cur_map.iloc[i]['매체']} / {cur_map.iloc[i]['캠페인']} → {cur_map.iloc[i]['전환컬럼']}({cur_map.iloc[i]['라벨']})")
            if st.button("삭제", type="secondary"):
                row = cur_map.iloc[del_idx]
                q("""DELETE FROM conversion_mapping
                     WHERE advertiser_code=? AND platform=? AND campaign=?""",
                  (adv_code, row["매체"], row["캠페인"]), fetch=False)
                st.success("삭제됨"); st.rerun()

# ============================================================
# 10) 페이지: 🏢 광고주 관리 (관리자 전용)
# ============================================================
elif page == "🏢 광고주 관리":
    st.title("🏢 광고주 관리")
    if not is_admin:
        st.error("관리자 권한이 필요합니다."); st.stop()
    
    advs = pd.read_sql("SELECT code AS 코드, name AS 이름, created_at AS 생성일 FROM advertisers ORDER BY created_at DESC",
                       sqlite3.connect(DB))
    st.subheader(f"등록된 광고주 ({len(advs)}개)")
    st.dataframe(advs, use_container_width=True, hide_index=True)
    
    st.divider()
    
    st.subheader("➕ 광고주 추가")
    with st.form("add_adv"):
        c1, c2 = st.columns(2)
        with c1: new_code = st.text_input("코드 (영문대문자, 예: GAME_C)")
        with c2: new_name = st.text_input("이름 (예: 게임사 C)")
        if st.form_submit_button("추가", type="primary"):
            if not new_code or not new_name:
                st.error("코드와 이름 모두 입력하세요.")
            else:
                try:
                    q("INSERT INTO advertisers (code,name) VALUES (?,?)",
                      (new_code.strip().upper(), new_name.strip()), fetch=False)
                    q("INSERT OR IGNORE INTO permissions VALUES (?,?,?)",
                      (user["email"], new_code.strip().upper(), "OWNER"), fetch=False)
                    st.success(f"'{new_name}' 추가 완료. 본인에게 OWNER 권한 부여됨."); st.rerun()
                except sqlite3.IntegrityError:
                    st.error("이미 존재하는 코드입니다.")
    
    st.divider()
    
    st.subheader("✏️ 이름 편집")
    if not advs.empty:
        edit_code = st.selectbox("편집할 광고주", advs["코드"].tolist(),
            format_func=lambda c: f"{c} — {advs[advs['코드']==c]['이름'].iloc[0]}")
        cur_name = advs[advs["코드"]==edit_code]["이름"].iloc[0]
        new_name2 = st.text_input("새 이름", value=cur_name, key="edit_name")
        if st.button("이름 변경"):
            q("UPDATE advertisers SET name=? WHERE code=?", (new_name2, edit_code), fetch=False)
            st.success("변경됨"); st.rerun()
    
    st.divider()
    
    st.subheader("🗑️ 광고주 삭제 (주의: 데이터·권한·매핑 모두 함께 삭제)")
    if not advs.empty:
        del_code = st.selectbox("삭제할 광고주", advs["코드"].tolist(), key="del_sel",
            format_func=lambda c: f"{c} — {advs[advs['코드']==c]['이름'].iloc[0]}")
        confirm = st.text_input(f"확인을 위해 코드 '{del_code}' 를 그대로 입력하세요")
        if st.button("영구 삭제", type="secondary"):
            if confirm == del_code:
                for tbl in ["perf","upload_log","conversion_mapping","permissions"]:
                    q(f"DELETE FROM {tbl} WHERE advertiser_code=?", (del_code,), fetch=False)
                q("DELETE FROM advertisers WHERE code=?", (del_code,), fetch=False)
                st.success("삭제됨"); st.rerun()
            else:
                st.error("코드가 일치하지 않습니다.")
