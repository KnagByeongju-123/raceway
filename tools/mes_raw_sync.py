# -*- coding: utf-8 -*-
"""
MES 코일(원소재) 동기화 v1.0  —  stock_ledger_sync.py 와 같은 폴더에 두고 실행
MES(Oracle) 소재 입고·투입·실사용  →  Supabase(stock_ledger 프로젝트) raw_receipt · raw_use · raw_inventory
재고수불부(stock_ledger) 와 RACEWAY 재고관리(raceway) 의 "MES 코일 잔량" 이 이 표를 읽는다.

  python mes_raw_sync.py              최근 7일 입고 + 최근 7일 사용분 갱신, 투입현황은 전체 재계산
  python mes_raw_sync.py --days 200   소급 (처음 한 번)
  python mes_raw_sync.py --ddl        표가 없을 때 만들 SQL 출력

접속 정보(Oracle · Supabase 키)는 stock_ledger_sync.py 의 값을 그대로 가져다 쓴다 (여기엔 안 적음).

표 (stock_ledger index.html 이 읽는 열 그대로)
  raw_receipt  : receipt_no, seq, receipt_date, raw_item_code, item_spec, heat_no, weight, vendor_site_id, supplier, io_flag
  raw_use      : use_key, use_date, machine_code, item_code, item_name, raw_item_code, heat_no, receipt_no,
                 unit_per_qty, use_weight, shot_qty, normal_shot, abnormal_shot
  raw_inventory: receipt_no, receipt_seq, heat_no, stock_status(1 장착중 / 2 투입완료 / 0 미투입), put_on_date, put_out_date,
                 issue_date, unit_weight, use_weight, item_code, machine_code, shot_qty
    · 투입현황은 입고(NET_WEIGHT) − 실사용(USE_WEIGHT 합) 으로 계산한다.
      잔량 0.5kg 이하 → 2(완료), 불출(IMTL_RAW_MATERIAL_ISSUE)됐고 잔량 남음 → 1(장착중), 그 외 0.
  raw_stock    : MES 소재 재고 표 IMTL_RAW_MATERIAL_STOCK 그대로 (item_code, qty, qty_unit, stock_weight_kg, last_receipt_date, last_modify_date)
                 → RACEWAY 앱의 "소재 잔량" 은 이 표를 1순위로 쓴다 (MES 관리도구 ⑤ 소재 관리가 맞춰 두는 값)
  raw_stock_lot: IMTL_RAW_MATERIAL_STOCK_LOT 그대로 (receipt_no, receipt_seq, heat_no, item_code, qty, unit_weight, total_weight, enter_date)
  입고 NET_WEIGHT 가 g 단위인 MES 도 있어 최근 1년 중앙값이 50,000 이상이면 g 로 보고 kg 로 바꾼다 (MES 관리도구와 같은 판정).
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

try:
    import stock_ledger_sync as base          # Oracle 접속 · SB2_URL · SB2_KEY 재사용
except ImportError:
    print("stock_ledger_sync.py 가 같은 폴더에 있어야 합니다")
    sys.exit(2)

SB_URL = base.SB2_URL
SB_KEY = base.SB2_KEY
REM_ZERO = 0.5          # 이 kg 이하면 소진으로 본다 (stock_ledger 와 동일)

DDL = """
create table if not exists raw_receipt (
  receipt_no text not null, seq integer not null default 1, receipt_date date, raw_item_code text, item_spec text,
  heat_no text, weight numeric, vendor_site_id text, supplier text, io_flag text default '1',
  synced_at timestamptz default now(), primary key (receipt_no, seq));
create table if not exists raw_use (
  use_key text primary key, use_date date, machine_code text, item_code text, item_name text, raw_item_code text,
  heat_no text, receipt_no text, receipt_seq integer, unit_per_qty numeric, use_weight numeric, shot_qty integer,
  normal_shot integer, abnormal_shot integer, start_date timestamptz, end_date timestamptz, synced_at timestamptz default now());
create table if not exists raw_stock (
  item_code text primary key, qty numeric, qty_unit text, stock_weight_kg numeric, last_receipt_date date,
  last_modify_date timestamptz, synced_at timestamptz default now());
create table if not exists raw_stock_lot (
  receipt_no text not null, receipt_seq integer not null default 1, heat_no text, item_code text, qty numeric,
  unit_weight numeric, total_weight numeric, enter_date timestamptz, last_modify_date timestamptz,
  synced_at timestamptz default now(), primary key (receipt_no, receipt_seq));
create table if not exists raw_inventory (
  receipt_no text not null, receipt_seq integer not null default 1, heat_no text, stock_status text, put_on_date date,
  put_out_date date, issue_date date, unit_weight numeric, use_weight numeric, item_code text, machine_code text,
  shot_qty integer, synced_at timestamptz default now(), primary key (receipt_no, receipt_seq));
alter table raw_receipt enable row level security;  alter table raw_use enable row level security;  alter table raw_inventory enable row level security;
alter table raw_stock enable row level security;  alter table raw_stock_lot enable row level security;
create policy anon_all on raw_stock for all using (true) with check (true);
create policy anon_all on raw_stock_lot for all using (true) with check (true);
create policy anon_all on raw_receipt for all using (true) with check (true);
create policy anon_all on raw_use for all using (true) with check (true);
create policy anon_all on raw_inventory for all using (true) with check (true);
"""


# ───────────────────────── Supabase ─────────────────────────
def sb(path, method="GET", data=None, prefer=None):
    hd = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}
    if prefer:
        hd["Prefer"] = prefer
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    req = urllib.request.Request(f"{SB_URL}/rest/v1/{path}", data=body, headers=hd, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def upsert(table, rows, key, chunk=200):
    """on_conflict 업서트. 표에 그 키 제약이 없으면 지우고 넣는다."""
    n = 0
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        st, body = sb(f"{table}?on_conflict={key}", "POST", part, "return=minimal,resolution=merge-duplicates")
        if st >= 300:
            raise RuntimeError(f"{table} upsert HTTP {st}: {body}")
        n += len(part)
    return n


def q(v):
    return '"' + str(v).replace('"', '') + '"'


# ───────────────────────── Oracle 읽기 ─────────────────────────
def d(x):
    return x.strftime("%Y-%m-%d") if isinstance(x, (datetime, date)) else (str(x)[:10] if x else None)


def ts(x):
    return x.isoformat() if isinstance(x, datetime) else None


_UNIT = {"g": False}


def detect_unit(cur):
    """입고 순중량이 g 인지: 최근 1년 중앙값 50,000 이상이면 g (MES 관리도구와 동일)"""
    try:
        cur.execute("SELECT MEDIAN(NET_WEIGHT) FROM IMTL_RAW_MATERIAL_RECEIPT WHERE IO_FLAG = 1 AND NET_WEIGHT > 0 AND RECEIPT_DATE >= SYSDATE - 365")
        med = cur.fetchone()[0]
        _UNIT["g"] = bool(med and med >= 50000)
    except Exception:
        _UNIT["g"] = False


def kg(v):
    v = float(v or 0)
    return v / 1000 if _UNIT["g"] else v


def fetch_stock(cur):
    cur.execute("""SELECT S.ITEM_CODE, NVL(S.QTY,0), S.QTY_UNIT, NVL(S.STOCK_WEIGHT_KG,0), S.LAST_RECEIPT_DATE, S.LAST_MODIFY_DATE
                     FROM IMTL_RAW_MATERIAL_STOCK S""")
    return [{"item_code": (r[0] or "").strip(), "qty": float(r[1]), "qty_unit": r[2] or "", "stock_weight_kg": float(r[3]),
             "last_receipt_date": d(r[4]), "last_modify_date": ts(r[5]), "synced_at": datetime.now().isoformat()} for r in cur.fetchall()]


def fetch_stock_lot(cur):
    cur.execute("""SELECT L.RECEIPT_NO, NVL(L.RECEIPT_SEQ,1), L.HEAT_NO, L.ITEM_CODE, NVL(L.QTY,0), L.UNIT_WEIGHT, L.TOTAL_WEIGHT,
                          L.ENTER_DATE, L.LAST_MODIFY_DATE
                     FROM IMTL_RAW_MATERIAL_STOCK_LOT L""")
    return [{"receipt_no": str(r[0]).strip(), "receipt_seq": int(r[1] or 1), "heat_no": (r[2] or "").strip(),
             "item_code": (r[3] or "").strip(), "qty": float(r[4]), "unit_weight": kg(r[5]), "total_weight": kg(r[6]),
             "enter_date": ts(r[7]), "last_modify_date": ts(r[8]), "synced_at": datetime.now().isoformat()} for r in cur.fetchall()]


def fetch_receipts(cur, since):
    cur.execute("""
        SELECT R.RECEIPT_NO, NVL(R.SEQ,1), R.RECEIPT_DATE, R.ITEM_CODE, R.HEAT_NO, NVL(R.NET_WEIGHT,0),
               R.VENDOR_SITE_ID, NVL(R.IO_FLAG,'1'), M.ITEM_NAME
          FROM IMTL_RAW_MATERIAL_RECEIPT R
          LEFT JOIN ICOM_ITEM_MASTER M ON M.ITEM_CODE = R.ITEM_CODE
         WHERE R.RECEIPT_DATE >= :s
         ORDER BY R.RECEIPT_DATE, R.RECEIPT_NO, R.SEQ""", s=since)
    out = []
    for r in cur.fetchall():
        out.append({"receipt_no": str(r[0]).strip(), "seq": int(r[1] or 1), "receipt_date": d(r[2]),
                    "raw_item_code": (r[3] or "").strip(), "item_spec": r[8] or "", "heat_no": (r[4] or "").strip(),
                    "weight": kg(r[5]), "vendor_site_id": r[6] or "", "supplier": r[6] or "",
                    "io_flag": str(r[7] or "1"), "synced_at": datetime.now().isoformat()})
    return out


def fetch_uses(cur, since):
    cur.execute("""
        SELECT D.RECEIPT_NO, NVL(D.RECEIPT_SEQ,1), D.HEAT_NO, D.ACTUCAL_DATE, D.WORK_ORDER_NO, D.ITEM_CODE,
               D.RAW_ITEM_CODE, NVL(D.UNIT_PER_QTY,0), NVL(D.USE_WEIGHT,0), NVL(D.SHOT_QTY,0),
               NVL(D.NORMAL_SHOT,0), NVL(D.ABNORMAL_SHOT,0), D.START_DATE, D.END_DATE, D.MACHINE_CODE, M.ITEM_NAME
          FROM IMTL_RAW_SF_USE_CASE_DTL D
          LEFT JOIN ICOM_ITEM_MASTER M ON M.ITEM_CODE = D.ITEM_CODE
         WHERE D.ACTUCAL_DATE >= :s
         ORDER BY D.ACTUCAL_DATE, D.START_DATE""", s=since)
    out = []
    for r in cur.fetchall():
        rno = str(r[0] or "").strip()
        key = "|".join([rno, str(int(r[1] or 1)), d(r[3]) or "", str(r[4] or ""), str(r[14] or ""), ts(r[12]) or ""])
        out.append({"use_key": key, "use_date": d(r[3]), "machine_code": r[14] or "", "item_code": (r[5] or "").strip(),
                    "item_name": r[15] or "", "raw_item_code": (r[6] or "").strip(), "heat_no": (r[2] or "").strip(),
                    "receipt_no": rno, "receipt_seq": int(r[1] or 1), "unit_per_qty": float(r[7]), "use_weight": float(r[8]),
                    "shot_qty": int(r[9]), "normal_shot": int(r[10]), "abnormal_shot": int(r[11]),
                    "start_date": ts(r[12]), "end_date": ts(r[13]), "synced_at": datetime.now().isoformat()})
    return out


def fetch_issues(cur, since):
    """불출(투입) 기록: 입고번호+항번별 첫 투입일"""
    cur.execute("""
        SELECT I.RECEIPT_NO, NVL(I.RECEIPT_SEQ,1), MIN(I.ENTER_DATE), MAX(I.MACHINE_CODE), MAX(I.ITEM_CODE)
          FROM IMTL_RAW_MATERIAL_ISSUE I
         WHERE I.ENTER_DATE >= :s
         GROUP BY I.RECEIPT_NO, NVL(I.RECEIPT_SEQ,1)""", s=since)
    return {(str(r[0]).strip(), int(r[1] or 1)): {"issue_date": d(r[2]), "machine_code": r[3] or "", "item_code": (r[4] or "").strip()}
            for r in cur.fetchall()}


def build_inventory(cur, since):
    """입고 − 실사용 으로 코일별 투입현황 계산 (입고 since 이후분 전체)"""
    rcv = fetch_receipts(cur, since)
    uses = fetch_uses(cur, since)
    iss = fetch_issues(cur, since)
    use_by = {}
    for u in uses:
        k = (u["receipt_no"], u["receipt_seq"])
        o = use_by.setdefault(k, {"wt": 0.0, "shot": 0, "mc": "", "pn": "", "first": None, "last": None})
        o["wt"] += u["use_weight"]; o["shot"] += u["shot_qty"]
        if u["machine_code"]: o["mc"] = u["machine_code"]
        if u["item_code"]: o["pn"] = u["item_code"]
        ud = u["use_date"]
        if ud and (o["first"] is None or ud < o["first"]): o["first"] = ud
        if ud and (o["last"] is None or ud > o["last"]): o["last"] = ud
    inv = []
    for r in rcv:
        if r["io_flag"] != "1":
            continue
        k = (r["receipt_no"], r["seq"])
        u = use_by.get(k, {"wt": 0.0, "shot": 0, "mc": "", "pn": "", "first": None, "last": None})
        i = iss.get(k, {})
        rem = r["weight"] - u["wt"]
        if rem <= REM_ZERO and (u["wt"] > 0 or i):
            st = "2"
        elif i or u["wt"] > 0:
            st = "1"
        else:
            st = "0"
        inv.append({"receipt_no": r["receipt_no"], "receipt_seq": r["seq"], "heat_no": r["heat_no"], "stock_status": st,
                    "put_on_date": i.get("issue_date") or u["first"], "put_out_date": u["last"] if st == "2" else None,
                    "issue_date": i.get("issue_date"), "unit_weight": r["weight"], "use_weight": round(u["wt"], 2),
                    "item_code": u["pn"] or i.get("item_code", ""), "machine_code": u["mc"] or i.get("machine_code", ""),
                    "shot_qty": u["shot"], "synced_at": datetime.now().isoformat()})
    return rcv, uses, inv


# ───────────────────────── 실행 ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="입고·사용 갱신 일수 (기본 7)")
    ap.add_argument("--inv-days", type=int, default=400, help="투입현황 재계산 범위: 최근 N일 입고 코일 (기본 400)")
    ap.add_argument("--ddl", action="store_true")
    a = ap.parse_args()
    if a.ddl:
        print(DDL); return 0
    since = datetime.now() - timedelta(days=a.days)
    inv_since = datetime.now() - timedelta(days=a.inv_days)
    conn = base.ora()
    try:
        cur = conn.cursor()
        detect_unit(cur)
        rcv = fetch_receipts(cur, since)
        uses = fetch_uses(cur, since)
        _, _, inv = build_inventory(cur, inv_since)
        stock, lots = [], []
        try:
            stock = fetch_stock(cur)
            lots = fetch_stock_lot(cur)
        except Exception as e:
            print(f"[MES 코일] 재고표(STOCK/STOCK_LOT) 읽기 실패 → 코일 계산만 올림: {e}")
        cur.close()
    finally:
        conn.close()
    n1 = upsert("raw_receipt", rcv, "receipt_no,seq")
    n2 = upsert("raw_use", uses, "use_key")
    n3 = upsert("raw_inventory", inv, "receipt_no,receipt_seq")
    n4 = n5 = 0
    if stock:
        sb("raw_stock?item_code=neq.", "DELETE", prefer="return=minimal")        # 전체 바꿔치기
        n4 = upsert("raw_stock", stock, "item_code")
    if lots:
        sb("raw_stock_lot?receipt_no=neq.", "DELETE", prefer="return=minimal")
        n5 = upsert("raw_stock_lot", lots, "receipt_no,receipt_seq")
    print(f"[MES 코일] 재고표 {n4}품번 ({sum(x['stock_weight_kg'] for x in stock):,.1f} kg) · 재고LOT {n5}건 · 입고 단위 {'g→kg' if _UNIT['g'] else 'kg'}")
    live = sum(1 for x in inv if x["stock_status"] != "2")
    kg = sum(x["unit_weight"] - x["use_weight"] for x in inv if x["stock_status"] != "2")
    print(f"[MES 코일] 입고 {n1}건 · 사용 {n2}건 · 투입현황 {n3}본 (잔량 있는 코일 {live}본 · {kg:,.1f} kg) → {SB_URL}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[MES 코일] 실패: {e}")
        sys.exit(2)
