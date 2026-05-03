# app.py — AdHub: 광고 리포팅 + 업로드 어드민 (단일 파일 데모)
import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime
import plotly.express as px

st.set_page_config(page_title="AdHub", page_icon="📊", layout="wide")

DB = "adhub.db"

def init_db():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        email TEXT PRIMARY KEY, name TEXT, role TEXT, password TEXT
    );
    CREATE TABLE IF NOT EXISTS advertisers (
        code TEXT PRIMARY KEY, name TEXT
    );
    CREATE TABLE IF NOT EXISTS permissions (
        email TEXT, advertiser_code TEXT, level TEXT,
        PRIMARY KEY (email, advertiser_code)
    );
    CREATE TABLE IF NOT EXISTS perf (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        advertiser_code TEXT, platform TEXT, date TEXT,
        campaign TEXT, adgroup TEXT,
        impressions INTEGER, clicks INTEGER, cost REAL, conversions REAL
    );
    CREATE TABLE IF NOT EXISTS upload_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT, advertiser_code TEXT, platform TEXT,
        file_name TEXT, rows INTEGER, uploaded_at TEXT
    );
    """)
    cur.executemany("INSERT OR IGNORE INTO users VALUES (?,?,?,?)", [
        ("admin@adhub.com",  "김에이전시", "AGENCY_ADMIN", "1234"),
        ("manager@scon.com", "박마케터",   "MANAGER",      "1234"),
        ("viewer@scon.com",  "최뷰어",     "VIEWER",       "1234"),
    ])
    cur.executemany("INSERT OR IGNORE INTO advertisers VALUES (?,?)", [
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

init_db()

def login_view():
    st.title("📊 AdHub 로그인")
    st.caption("데모 계정: admin@adhub.com / manager@scon.com / viewer@scon.com (비밀번호: 1234)")
    email = st.text_input("이메일")
    pw = st.text_input("비밀번호", type="password")
    if st.button("로그인", type="primary"):
        con = sqlite3.connect(DB)
        row = con.execute("SELECT email,name,role FROM users WHERE email=? AND password=?",
                          (email, pw)).fetchone()
        con.close()
        if row:
            st.session_state.user = {"email": row[0], "name": row[1], "role": row[2]}
            st.rerun()
        else:
            st.error("로그인 실패")

if "user" not in st.session_state:
    login_view(); st.stop()

user = st.session_state.user

con = sqlite3.connect(DB)
my_advs = con.execute("""
    SELECT a.code, a.name, p.level
    FROM permissions p JOIN advertisers a ON a.code=p.advertiser_code
    WHERE p.email=?""", (user["email"],)).fetchall()
con.close()

with st.sidebar:
    st.markdown(f"**👤 {user['name']}**  \n`{user['role']}`")
    if st.button("로그아웃"): del st.session_state.user; st.rerun()
    st.divider()
    adv_options = {f"{name} ({code})": (code, level) for code, name, level in my_advs}
    sel = st.selectbox("광고주 선택", list(adv_options.keys()))
    adv_code, my_level = adv_options[sel]
    st.info(f"권한: **{my_level}**")
    page = st.radio("메뉴", ["📈 대시보드", "📤 데이터 업로드", "📋 업로드 이력"])

GOOGLE_MAP = {"일":"date","캠페인":"campaign","광고그룹":"adgroup",
              "노출수":"impressions","클릭수":"clicks","비용":"cost","전환수":"conversions"}
FB_MAP = {"보고 시작":"date","캠페인 이름":"campaign",
          "노출":"impressions","클릭(전체)":"clicks","지출 금액 (KRW)":"cost",
          "랜딩 페이지 조회":"conversions"}

def parse_file(file, platform):
    df = pd.read_excel(file) if file.name.endswith(("xlsx","xls")) else pd.read_csv(file)
    mapping = GOOGLE_MAP if platform == "GOOGLE" else FB_MAP
    rename = {k: v for k, v in mapping.items() if k in df.columns}
    df = df.rename(columns=rename)
    if "adgroup" not in df.columns and "campaign" in df.columns:
        df["adgroup"] = df["campaign"]
    needed = ["date","campaign","adgroup","impressions","clicks","cost","conversions"]
    for c in needed:
        if c not in df.columns: df[c] = 0
    df = df[needed].dropna(subset=["date"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return df.dropna(subset=["date"])

if page == "📈 대시보드":
    st.title(f"📈 {sel} — 성과 대시보드")
    con = sqlite3.connect(DB)
    df = pd.read_sql("SELECT * FROM perf WHERE advertiser_code=?", con, params=(adv_code,))
    con.close()
    if df.empty:
        st.warning("데이터가 없습니다. '데이터 업로드' 메뉴에서 파일을 올려주세요.")
    else:
        c1,c2,c3,c4 = st.columns(4)
        c1.metric("총 광고비", f"₩{df['cost'].sum():,.0f}")
        c2.metric("총 노출",   f"{df['impressions'].sum():,}")
        c3.metric("총 클릭",   f"{df['clicks'].sum():,}")
        ctr = df['clicks'].sum()/max(df['impressions'].sum(),1)*100
        c4.metric("CTR",       f"{ctr:.2f}%")
        st.divider()
        daily = df.groupby(["date","platform"], as_index=False)["cost"].sum()
        st.plotly_chart(px.line(daily, x="date", y="cost", color="platform",
                                title="일자별 광고비 추이"), use_container_width=True)
        st.dataframe(df, use_container_width=True, height=400)

elif page == "📤 데이터 업로드":
    st.title("📤 로우데이터 업로드")
    if my_level == "VIEWER":
        st.error("⛔ VIEWER 권한은 업로드할 수 없습니다."); st.stop()
    
    platform = st.radio("매체 선택", ["GOOGLE", "FACEBOOK"], horizontal=True)
    file = st.file_uploader("파일 업로드 (xlsx / csv)", type=["xlsx","xls","csv"])
    
    if file:
        try:
            df = parse_file(file, platform)
            st.success(f"✅ 파싱 성공: {len(df)}행")
            st.dataframe(df.head(10), use_container_width=True)
            
            if st.button("🚀 DB에 저장", type="primary"):
                con = sqlite3.connect(DB)
                for _, r in df.iterrows():
                    con.execute("""INSERT INTO perf 
                        (advertiser_code,platform,date,campaign,adgroup,
                         impressions,clicks,cost,conversions)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                        (adv_code, platform, r["date"], r["campaign"], r["adgroup"],
                         int(r["impressions"] or 0), int(r["clicks"] or 0),
                         float(r["cost"] or 0), float(r["conversions"] or 0)))
                con.execute("""INSERT INTO upload_log 
                    (email,advertiser_code,platform,file_name,rows,uploaded_at)
                    VALUES (?,?,?,?,?,?)""",
                    (user["email"], adv_code, platform, file.name, len(df),
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                con.commit(); con.close()
                st.success(f"🎉 {len(df)}행이 저장되었습니다!")
                st.balloons()
        except Exception as e:
            st.error(f"파싱 오류: {e}")

else:
    st.title("📋 업로드 이력")
    con = sqlite3.connect(DB)
    logs = pd.read_sql("""SELECT uploaded_at, email, platform, file_name, rows 
                          FROM upload_log WHERE advertiser_code=? 
                          ORDER BY id DESC""", con, params=(adv_code,))
    con.close()
    st.dataframe(logs, use_container_width=True)
