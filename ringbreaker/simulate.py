"""Synthetic UPI / IMPS ecosystem with planted money-mule networks.

Everything is fictional: banks, account ids, VPAs, devices. Timestamps are IST.

What makes the data realistic enough to be interesting:
  * Normal life: salaries, rent, merchant spend, P2P with friends, gig payouts,
    parents funding students, ATM withdrawals.
  * Hard negatives that *look* like mules: employers (huge fan-out), merchants
    (huge fan-in + daily sweeps), landlords (fan-in), students who receive money
    and pay college fees within the hour (fast pass-through), trip/chit
    organisers who collect from many and pay one, families sharing a phone.
  * Four mule typologies with ground-truth labels (ring_id, typology, role).
  * Complaints like India's NCRP portal: only ~40% of victims report, days later,
    and only the first-hop mule is named. That is the label a bank actually has.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

START = pd.Timestamp("2026-08-01 00:00:00")
DAYS = 61  # 2026-08-01 .. 2026-09-30
SEED = 7

BANKS = [
    ("KVR", "Kaveri Bank", 0.24), ("DCN", "Deccan Trust Bank", 0.20), ("CRM", "Coromandel Bank", 0.17),
    ("IND", "Indus Valley Bank", 0.15), ("MLB", "Malabar Co-op Bank", 0.12), ("NMD", "Narmada SFB", 0.12),
]
CITIES = ["Chennai", "Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Kolkata", "Pune", "Kochi",
          "Jaipur", "Lucknow", "Patna", "Ranchi", "Guwahati", "Indore", "Coimbatore"]
SEGMENTS = {  # share, P2M per day, P2P per day, income (monthly, INR)
    "salaried": (0.34, 0.55, 0.12, 65000),
    "student": (0.14, 0.40, 0.10, 0),
    "self_employed": (0.14, 0.50, 0.15, 55000),
    "gig_worker": (0.10, 0.45, 0.10, 28000),
    "homemaker": (0.10, 0.35, 0.08, 0),
    "retired": (0.08, 0.30, 0.06, 30000),
}
MCC = {"Grocery": 600, "Restaurants": 450, "Fuel": 1200, "Electronics": 9000, "Pharmacy": 500,
       "Apparel": 1800, "Utilities": 1500, "Education": 25000, "Travel": 6000, "Crypto OTC": 40000}
SCAM_TYPES = ["Investment scam", "Task / part-time job scam", "Digital arrest", "Fake loan app",
              "OLX / marketplace fraud", "KYC update phishing"]

# hourly activity profile (IST), normal people
HOUR_P = np.array([.4, .2, .15, .1, .15, .4, 1.2, 2.2, 3.2, 3.6, 3.9, 4.0, 4.2, 4.0, 3.7, 3.5, 3.6, 4.0,
                   4.6, 5.0, 4.8, 3.8, 2.3, 1.1])
HOUR_P = HOUR_P / HOUR_P.sum()


@dataclass
class World:
    rng: np.random.Generator
    accounts: list[dict] = field(default_factory=list)
    acct_devices: list[tuple] = field(default_factory=list)
    devices: dict = field(default_factory=dict)
    txns: list[tuple] = field(default_factory=list)
    complaints: list[dict] = field(default_factory=list)
    _dev_n: int = 0

    # -- helpers ------------------------------------------------------------------
    def new_device(self, os_name=None, ip_prefix=None, first_seen=None) -> str:
        self._dev_n += 1
        did = f"D{self._dev_n:06d}"
        self.devices[did] = {
            "device_id": did,
            "os": os_name or self.rng.choice(["Android", "Android", "Android", "iOS"]),
            "ip_prefix": ip_prefix or f"{self.rng.integers(27, 223)}.{self.rng.integers(0, 255)}.{self.rng.integers(0, 255)}",
            "first_seen": first_seen or START - pd.Timedelta(days=int(self.rng.integers(30, 900))),
        }
        return did

    def add_account(self, **kw) -> dict:
        i = len(self.accounts) + 1
        bank = kw.pop("bank", None) or self.rng.choice([b[0] for b in BANKS], p=[b[2] for b in BANKS])
        a = {
            "account_id": f"A{i:06d}", "bank": bank, "vpa": f"u{i:06d}@{bank.lower()}",
            "segment": "salaried", "kyc": "FULL", "opened_date": START - pd.Timedelta(days=int(self.rng.integers(200, 3500))),
            "city": self.rng.choice(CITIES), "age": int(self.rng.integers(21, 70)), "mcc": None,
            "is_mule": 0, "ring_id": None, "typology": None, "role": "normal", "is_victim": 0,
        }
        a.update(kw)
        self.accounts.append(a)
        return a

    def link(self, acct_id, device_id):
        self.acct_devices.append((acct_id, device_id))

    def tx(self, ts, src, dst, amount, channel, device=None, typ="P2P"):
        self.txns.append((ts, src, dst, round(float(amount), 2), channel, device, typ))

    def ts(self, day: int, hour: int | None = None, minute_jitter=True) -> pd.Timestamp:
        h = int(self.rng.choice(24, p=HOUR_P)) if hour is None else hour
        m = int(self.rng.integers(0, 60)) if minute_jitter else 0
        return START + pd.Timedelta(days=day, hours=h, minutes=m, seconds=int(self.rng.integers(0, 60)))


def _amt(rng, mean, sigma=0.7, lo=10, hi=200000):
    return float(np.clip(rng.lognormal(np.log(mean), sigma), lo, hi))


def simulate(n_people: int = 13000, n_merchants: int = 900, seed: int = SEED) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    w = World(rng)

    # ---------------- institutions ----------------------------------------------
    employers = [w.add_account(segment="corporate", age=None, opened_date=START - pd.Timedelta(days=4000))
                 for _ in range(25)]
    platforms = [w.add_account(segment="corporate", age=None, opened_date=START - pd.Timedelta(days=2500))
                 for _ in range(6)]  # gig platforms
    for a in employers + platforms:
        d = w.new_device(os_name="Server", ip_prefix=f"10.{rng.integers(0, 255)}.{rng.integers(0, 255)}")
        w.link(a["account_id"], d)
        a["_dev"] = d

    cats = list(MCC)
    cat_p = np.array([.22, .17, .12, .05, .10, .12, .10, .06, .05, .01])
    merchants = []
    for _ in range(n_merchants):
        c = rng.choice(cats, p=cat_p / cat_p.sum())
        m = w.add_account(segment="merchant", mcc=c, age=None)
        d = w.new_device()
        w.link(m["account_id"], d)
        m["_dev"] = d
        # merchants sweep to a settlement account they own (same bank)
        m["_sweep"] = w.add_account(segment="merchant_settlement", bank=m["bank"], city=m["city"], age=None)
        w.link(m["_sweep"]["account_id"], d)
        merchants.append(m)
    by_cat = {c: [m for m in merchants if m["mcc"] == c] for c in cats}
    otc = by_cat["Crypto OTC"]

    # ---------------- people -----------------------------------------------------
    segs = list(SEGMENTS)
    seg_p = np.array([SEGMENTS[s][0] for s in segs])
    people = []
    for _ in range(n_people):
        s = rng.choice(segs, p=seg_p / seg_p.sum())
        age = int(rng.integers(18, 24)) if s == "student" else int(rng.integers(60, 80)) if s == "retired" \
            else int(rng.integers(22, 60))
        p = w.add_account(segment=s, age=age, kyc="MIN" if rng.random() < (0.25 if s == "student" else 0.06) else "FULL")
        people.append(p)
    # devices: families share phones sometimes (hard negative for device features)
    i = 0
    while i < len(people):
        fam = 1 if rng.random() > 0.08 else int(rng.integers(2, 4))
        d = w.new_device()
        for p in people[i:i + fam]:
            w.link(p["account_id"], d)
            p["_dev"] = d
        i += fam
    for p in people:
        if rng.random() < 0.07:  # new-to-bank customer, opened recently on a new phone
            opened = START + pd.Timedelta(days=int(rng.integers(-120, 40)))
            p["opened_date"] = opened
            d = w.new_device(first_seen=opened)
            w.link(p["account_id"], d)
            p["_dev"] = d
        elif rng.random() < 0.11:  # bought a new phone during the period
            d = w.new_device(first_seen=START + pd.Timedelta(days=int(rng.integers(0, 55))))
            w.link(p["account_id"], d)
            p["_dev"] = d

    ids = np.array([p["account_id"] for p in people])
    # friend graph
    friends = {p["account_id"]: list(rng.choice(ids, int(rng.integers(4, 14)), replace=False)) for p in people}
    pmap = {p["account_id"]: p for p in people}

    salaried = [p for p in people if p["segment"] == "salaried"]
    students = [p for p in people if p["segment"] == "student"]
    gig = [p for p in people if p["segment"] == "gig_worker"]
    for p in salaried + [p for p in people if p["segment"] == "self_employed"]:
        p["_income"] = SEGMENTS[p["segment"]][3] * rng.lognormal(0, 0.45)
    for p in salaried:
        p["_employer"] = employers[int(rng.integers(0, len(employers)))]

    # ---------------- normal flows ---------------------------------------------------
    for day in (0, 31):  # salaries on 1 Aug and 1 Sep
        for p in salaried:
            e = p["_employer"]
            w.tx(w.ts(day, hour=int(rng.integers(9, 12))), e["account_id"], p["account_id"], p["_income"], "IMPS", e["_dev"], "SALARY")
    # some earners forward most of the salary to family / own savings within hours (legit pass-through)
    for p in rng.choice(salaried, int(len(salaried) * 0.08), replace=False):
        fam_acct = people[int(rng.integers(0, len(people)))]
        for day in (0, 31):
            t0 = START + pd.Timedelta(days=day, hours=int(rng.integers(12, 20)), minutes=int(rng.integers(0, 60)))
            w.tx(t0, p["account_id"], fam_acct["account_id"], p["_income"] * rng.uniform(.4, .9), "IMPS", p["_dev"], "P2P")
    # small traders: collect from many customers by UPI, pay suppliers the same day
    traders = list(rng.choice([p for p in people if p["segment"] == "self_employed"], 180, replace=False))
    suppliers = list(rng.choice(merchants, 60, replace=False))
    for tr in traders:
        sup = suppliers[int(rng.integers(0, len(suppliers)))]
        for day in range(DAYS):
            if rng.random() < 0.55:
                tot = 0.0
                for _ in range(int(rng.integers(2, 9))):
                    a = _amt(rng, 900, 0.7)
                    c = people[int(rng.integers(0, len(people)))]
                    w.tx(w.ts(day, hour=int(rng.integers(9, 20))), c["account_id"], tr["account_id"], a, "UPI", c["_dev"], "P2P")
                    tot += a
                w.tx(w.ts(day, hour=int(rng.integers(20, 23))), tr["account_id"], sup["account_id"], tot * rng.uniform(.7, .95), "UPI", tr["_dev"], "P2M")

    # rent: landlords collect from tenants (fan-in hard negative)
    landlords = list(rng.choice([p for p in people if p["segment"] in ("self_employed", "retired")], 220, replace=False))
    tenants = list(rng.choice(salaried + gig, 1500, replace=False))
    for t in tenants:
        t["_landlord"] = landlords[int(rng.integers(0, len(landlords)))]
        t["_rent"] = float(rng.choice([8000, 12000, 15000, 18000, 22000, 30000]))
    for day0 in (1, 32):
        for t in tenants:
            day = day0 + int(rng.integers(0, 5))
            w.tx(w.ts(day), t["account_id"], t["_landlord"]["account_id"], t["_rent"], "UPI", t["_dev"], "RENT")
    # students: parents send money; some pay college fees within the hour (fast pass-through, legit)
    parents = salaried + [p for p in people if p["segment"] == "self_employed"]
    edu = by_cat["Education"]
    for s in students:
        par = parents[int(rng.integers(0, len(parents)))]
        for day0 in (2, 33):
            day = day0 + int(rng.integers(0, 6))
            t0 = w.ts(day, hour=int(rng.integers(8, 21)))
            amt = _amt(rng, 6000, 0.5)
            w.tx(t0, par["account_id"], s["account_id"], amt, "UPI", par["_dev"], "P2P")
        if rng.random() < 0.18:  # fee payment month
            day = int(rng.integers(10, 50))
            t0 = w.ts(day, hour=int(rng.integers(9, 18)))
            fee = float(rng.choice([35000, 48000, 62000, 85000]))
            w.tx(t0, par["account_id"], s["account_id"], fee, "IMPS", par["_dev"], "P2P")
            m = edu[int(rng.integers(0, len(edu)))]
            w.tx(t0 + pd.Timedelta(minutes=int(rng.integers(5, 55))), s["account_id"], m["account_id"], fee, "UPI", s["_dev"], "P2M")
    # gig payouts: many small inflows from platforms
    for g in gig:
        plat = platforms[int(rng.integers(0, len(platforms)))]
        for day in range(DAYS):
            if rng.random() < 0.75:
                for _ in range(int(rng.integers(1, 4))):
                    w.tx(w.ts(day, hour=int(rng.integers(10, 23))), plat["account_id"], g["account_id"], _amt(rng, 450, 0.5), "UPI", plat["_dev"], "PAYOUT")
    # trip / chit organisers: collect from many in a day, pay one vendor (legit fan-in -> fan-out)
    organisers = list(rng.choice(people, 60, replace=False))
    travel = by_cat["Travel"]
    for o in organisers:
        day = int(rng.integers(3, 55))
        pool = list(dict.fromkeys(friends[o["account_id"]] + list(rng.choice(ids, 12))))
        payers = [x for x in rng.choice(pool, min(len(pool), int(rng.integers(6, 16))), replace=False) if x != o["account_id"]]
        share = float(rng.choice([2500, 4000, 6000, 9000]))
        for pid in payers:
            w.tx(w.ts(day, hour=int(rng.integers(9, 22))), pid, o["account_id"], share, "UPI", pmap[pid]["_dev"], "P2P")
        v = travel[int(rng.integers(0, len(travel)))]
        w.tx(w.ts(day + 1, hour=int(rng.integers(10, 20))), o["account_id"], v["account_id"], share * len(payers) * 0.97, "IMPS", o["_dev"], "P2M")

    # daily spend + P2P + ATM (vectorised per person)
    w_cat = np.array([.26, .2, .14, .03, .11, .13, .11, 0, .02, 0])
    w_cat = w_cat / w_cat.sum()
    def background(p, scale=1.0, day_from=0, day_to=DAYS):
        """Everyday life: merchant spend, P2P with friends, ATM. Mules get it too (camouflage)."""
        _, r_m, r_p, _ = SEGMENTS.get(p["segment"], SEGMENTS["self_employed"])
        span = max(day_to - day_from, 1)
        n_m = rng.poisson(r_m * span * scale)
        cs = rng.choice(cats, n_m, p=w_cat)
        for d, c in zip(rng.integers(day_from, day_to, n_m), cs):
            m = by_cat[c][int(rng.integers(0, len(by_cat[c])))]
            w.tx(w.ts(int(d)), p["account_id"], m["account_id"], _amt(rng, MCC[c], 0.8), "UPI", p["_dev"], "P2M")
        fr = friends.get(p["account_id"]) or list(rng.choice(ids, 6))
        for d in rng.integers(day_from, day_to, rng.poisson(r_p * span * scale)):
            w.tx(w.ts(int(d)), p["account_id"], fr[int(rng.integers(0, len(fr)))], _amt(rng, 1200, 0.9), "UPI", p["_dev"], "P2P")
        for d in rng.integers(day_from, day_to, rng.poisson(0.06 * span * scale)):
            w.tx(w.ts(int(d), hour=int(rng.integers(8, 22))), p["account_id"], "ATM", float(rng.choice([500, 1000, 2000, 3000, 5000])), "ATM", p["_dev"], "CASH")

    for p in people:
        # ~12% are light users (a handful of transactions a month -> long natural gaps)
        background(p, scale=0.12 if rng.random() < 0.12 else 1.0)
    # merchant daily sweeps to own settlement account (legit pass-through, slow dwell)
    # done after fraud generation so sweeps include everything; amounts computed later from ledger
    ring_n = 0

    def mule_accounts(n, ring_id, typology, role, opened_days_before, segs=("student", "gig_worker", "homemaker"),
                      kyc_min_p=0.55, shared_devices=None, ip=None, opened_spread=40):
        out = []
        for k in range(n):
            opened = START + pd.Timedelta(days=int(opened_days_before) - int(rng.integers(0, opened_spread)))
            rented = rng.random() < 0.3 and not shared_devices
            if rented:  # an ordinary customer renting out an old account for commission
                opened = START - pd.Timedelta(days=int(rng.integers(400, 2500)))
            a = w.add_account(segment=str(rng.choice(segs)), age=int(rng.integers(19, 48 if rented else 32)),
                              kyc="MIN" if rng.random() < kyc_min_p else "FULL", opened_date=opened,
                              is_mule=1, ring_id=ring_id, typology=typology, role=role)
            if shared_devices:
                d = shared_devices[k % len(shared_devices)]
            else:
                d = w.new_device(ip_prefix=ip, first_seen=opened if not rented else
                                 START - pd.Timedelta(days=int(rng.integers(60, 600))))
            w.link(a["account_id"], d)
            a["_dev"] = d
            a["_slow"] = rng.random() < 0.25  # holds funds for hours before moving them
            # everyday activity: rented accounts live a full normal life, fresh ones a thinner one
            first_day = max(0, (opened - START).days)
            background(a, scale=1.0 if rented else float(rng.uniform(0.2, 0.7)), day_from=first_day)
            out.append(a)
        return out

    def complain(victim, txn_ts, mule, amount, scam):
        if rng.random() < 0.40:  # most victims never report
            lag = pd.Timedelta(hours=int(rng.integers(2, 240)))
            w.complaints.append({"reported_at": txn_ts + lag, "victim_account": victim["account_id"],
                                 "reported_payee": mule["account_id"], "amount": round(amount, 2),
                                 "txn_ts": txn_ts, "category": scam})

    victims_pool = [p for p in people if p["segment"] in ("salaried", "self_employed", "retired")]

    def victim():
        v = victims_pool[int(rng.integers(0, len(victims_pool)))]
        v["is_victim"] = 1
        return v

    def cash_out(src, t0, total):
        """Layer-2 / cash-out behaviour: ATM bursts + crypto OTC."""
        left = total
        t = t0
        while left > 3000:
            t = t + pd.Timedelta(minutes=int(rng.integers(3, 50)))
            if rng.random() < 0.55:
                a = float(min(left, rng.choice([5000, 8000, 10000])))
                w.tx(t, src["account_id"], "ATM", a, "ATM", src["_dev"], "CASH")
            else:
                a = float(min(left, _amt(rng, 45000, 0.4)))
                m = otc[int(rng.integers(0, len(otc)))]
                w.tx(t, src["account_id"], m["account_id"], a, "UPI", src["_dev"], "P2M")
            left -= a

    # ---------------- typology 1: fan-in / fan-out layered rings -----------------------
    for _ in range(16):
        ring_n += 1
        rid = f"R{ring_n:02d}"
        start = int(rng.integers(4, 52))
        span = int(rng.integers(3, 9))
        farm = [w.new_device(first_seen=START + pd.Timedelta(days=start - 20))] if rng.random() < 0.4 else None
        l1 = mule_accounts(int(rng.integers(4, 9)), rid, "Layered fan-out", "L1 mule", start - 10, shared_devices=farm)
        l2 = mule_accounts(int(rng.integers(2, 4)), rid, "Layered fan-out", "L2 cash-out", start - 25,
                           segs=("self_employed", "gig_worker"), kyc_min_p=0.2)
        scam = str(rng.choice(SCAM_TYPES))
        l2_bal = {a["account_id"]: [] for a in l2}
        for _v in range(int(rng.integers(18, 55))):
            v = victim()
            mule = l1[int(rng.integers(0, len(l1)))]
            day = start + int(rng.integers(0, span))
            t0 = w.ts(day, hour=int(rng.integers(9, 23)))
            for _k in range(int(rng.choice([1, 1, 2, 3]))):
                amt = float(rng.choice([4999, 9999, 15000, 24999, 49999, 75000, 99000]) * rng.uniform(.8, 1))
                w.tx(t0, v["account_id"], mule["account_id"], amt, "UPI" if amt < 100000 else "IMPS", v["_dev"], "P2P")
                complain(v, t0, mule, amt, scam)
                # L1 forwards fast, keeps a commission
                fwd = amt * rng.uniform(.88, .98)
                parts = int(rng.integers(1, 3))
                tf = t0 + (pd.Timedelta(minutes=int(rng.integers(180, 1800))) if mule["_slow"]
                           else pd.Timedelta(minutes=int(rng.integers(4, 95))))
                for _pp in range(parts):
                    tgt = l2[int(rng.integers(0, len(l2)))]
                    w.tx(tf, mule["account_id"], tgt["account_id"], fwd / parts, "IMPS", mule["_dev"], "P2P")
                    l2_bal[tgt["account_id"]].append((tf, fwd / parts))
                    tf += pd.Timedelta(minutes=int(rng.integers(1, 12)))
                t0 += pd.Timedelta(minutes=int(rng.integers(10, 120)))
        for a in l2:
            batches = sorted(l2_bal[a["account_id"]])
            # cash out in a few batches
            for j in range(0, len(batches), 4):
                chunk = batches[j:j + 4]
                cash_out(a, chunk[-1][0] + pd.Timedelta(minutes=int(rng.integers(10, 180))), sum(x[1] for x in chunk) * .99)

    # ---------------- typology 2: device farm (many accounts, few phones) ---------------
    for _ in range(4):
        ring_n += 1
        rid = f"R{ring_n:02d}"
        start = int(rng.integers(8, 48))
        ip = f"{rng.integers(100, 200)}.{rng.integers(0, 255)}.{rng.integers(0, 255)}"
        devs = [w.new_device(os_name="Android", ip_prefix=ip, first_seen=START + pd.Timedelta(days=start - 15)) for _ in range(int(rng.integers(2, 4)))]
        farm = mule_accounts(int(rng.integers(10, 19)), rid, "Device farm", "Farm mule", start - 12, shared_devices=devs, ip=ip, opened_spread=6)
        agg = mule_accounts(1, rid, "Device farm", "Aggregator", start - 60, segs=("self_employed",), kyc_min_p=0)[0]
        scam = str(rng.choice(["Task / part-time job scam", "Investment scam"]))
        for _v in range(int(rng.integers(40, 90))):
            v = victim()
            m = farm[int(rng.integers(0, len(farm)))]
            t0 = w.ts(start + int(rng.integers(0, 12)), hour=int(rng.integers(10, 23)))
            amt = _amt(rng, 6000, 0.6, 500, 60000)
            w.tx(t0, v["account_id"], m["account_id"], amt, "UPI", v["_dev"], "P2P")
            complain(v, t0, m, amt, scam)
            w.tx(t0 + pd.Timedelta(minutes=int(rng.integers(15, 300))), m["account_id"], agg["account_id"], amt * .95, "UPI", m["_dev"], "P2P")
        total_in = sum(t[3] for t in w.txns if t[2] == agg["account_id"])
        cash_out(agg, START + pd.Timedelta(days=start + 12, hours=20), total_in * .98)

    # ---------------- typology 3: dormant account takeover ------------------------------
    for _ in range(5):
        ring_n += 1
        rid = f"R{ring_n:02d}"
        start = int(rng.integers(15, 55))
        accts = []
        for _k in range(int(rng.integers(3, 6))):
            a = w.add_account(segment=str(rng.choice(["retired", "homemaker", "salaried"])), age=int(rng.integers(35, 75)),
                              kyc="FULL", opened_date=START - pd.Timedelta(days=int(rng.integers(1500, 4000))),
                              is_mule=1, ring_id=rid, typology="Dormant takeover", role="Hijacked dormant")
            old = w.new_device(first_seen=START - pd.Timedelta(days=1400))
            w.link(a["account_id"], old)
            new = w.new_device(first_seen=START + pd.Timedelta(days=start - 1))  # new phone at takeover
            w.link(a["account_id"], new)
            a["_dev"] = new
            accts.append(a)
        sink = mule_accounts(1, rid, "Dormant takeover", "L2 cash-out", start - 30, segs=("self_employed",))[0]
        scam = str(rng.choice(SCAM_TYPES))
        for a in accts:
            for _v in range(int(rng.integers(5, 14))):
                v = victim()
                t0 = w.ts(start + int(rng.integers(0, 4)), hour=int(rng.integers(9, 23)))
                amt = _amt(rng, 30000, 0.5, 2000, 150000)
                w.tx(t0, v["account_id"], a["account_id"], amt, "IMPS", v["_dev"], "P2P")
                complain(v, t0, a, amt, scam)
                w.tx(t0 + pd.Timedelta(minutes=int(rng.integers(6, 70))), a["account_id"], sink["account_id"], amt * .97, "IMPS", a["_dev"], "P2P")
        tot = sum(t[3] for t in w.txns if t[2] == sink["account_id"])
        cash_out(sink, START + pd.Timedelta(days=start + 4, hours=1), tot * .98)

    # ---------------- typology 4: circular layering (round-tripping) ------------------
    for _ in range(3):
        ring_n += 1
        rid = f"R{ring_n:02d}"
        start = int(rng.integers(5, 50))
        cyc = mule_accounts(int(rng.integers(4, 7)), rid, "Circular layering", "Layering node", start - 90,
                            segs=("self_employed",), kyc_min_p=0.1, opened_spread=200)
        seed_in = w.add_account(segment="self_employed", is_mule=1, ring_id=rid, typology="Circular layering", role="Funding source")
        d = w.new_device()
        w.link(seed_in["account_id"], d)
        seed_in["_dev"] = d
        amt = _amt(rng, 180000, 0.3, 60000, 500000)
        t = w.ts(start, hour=11)
        w.tx(t, seed_in["account_id"], cyc[0]["account_id"], amt, "IMPS", d, "P2P")
        for _round in range(int(rng.integers(4, 9))):
            for k in range(len(cyc)):
                t = t + pd.Timedelta(minutes=int(rng.integers(20, 240)))
                amt *= rng.uniform(.985, .999)
                w.tx(t, cyc[k]["account_id"], cyc[(k + 1) % len(cyc)]["account_id"], amt, "IMPS", cyc[k]["_dev"], "P2P")
            t += pd.Timedelta(hours=int(rng.integers(6, 40)))
        w.tx(t + pd.Timedelta(hours=2), cyc[-1]["account_id"], seed_in["account_id"], amt * .97, "IMPS", cyc[-1]["_dev"], "P2P")

    # ---------------- assemble ---------------------------------------------------------
    tx = pd.DataFrame(w.txns, columns=["ts", "src", "dst", "amount", "channel", "device_id", "txn_type"])
    tx = tx[(tx.ts >= START) & (tx.ts < START + pd.Timedelta(days=DAYS))]
    tx = tx[tx.src != tx.dst]

    # merchant sweeps: each evening, move the day's takings to settlement (legit fast pass-through)
    p2m = tx[tx.dst.isin({m["account_id"] for m in merchants})].copy()
    p2m["d"] = p2m.ts.dt.normalize()
    sweeps = p2m.groupby(["dst", "d"]).amount.sum().reset_index()
    sweeps = sweeps[sweeps.amount > 2000]
    mdev = {m["account_id"]: (m["_dev"], m["_sweep"]["account_id"]) for m in merchants}
    sw = pd.DataFrame({
        "ts": sweeps.d + pd.Timedelta(hours=23, minutes=30),
        "src": sweeps.dst, "dst": [mdev[a][1] for a in sweeps.dst],
        "amount": (sweeps.amount * 0.985).round(2), "channel": "IMPS",
        "device_id": [mdev[a][0] for a in sweeps.dst], "txn_type": "SWEEP"})
    tx = pd.concat([tx, sw], ignore_index=True).sort_values("ts").reset_index(drop=True)

    # a sprinkle of failures, as in real switches
    tx["status"] = np.where(rng.random(len(tx)) < 0.012, "FAILED", "SUCCESS")
    tx.insert(0, "txn_id", [f"T{i:08d}" for i in range(len(tx))])
    dev_ip = {k: v["ip_prefix"] for k, v in w.devices.items()}
    tx["ip_prefix"] = tx.device_id.map(dev_ip)

    acc = pd.DataFrame([{k: v for k, v in a.items() if not k.startswith("_")} for a in w.accounts])
    dev = pd.DataFrame(list(w.devices.values()))
    ad = pd.DataFrame(w.acct_devices, columns=["account_id", "device_id"]).drop_duplicates()
    comp = pd.DataFrame(w.complaints).sort_values("reported_at").reset_index(drop=True)
    comp.insert(0, "complaint_id", [f"NCRP{2026_0000 + i}" for i in range(len(comp))])
    comp = comp[comp.reported_at < START + pd.Timedelta(days=DAYS + 10)]

    truth = acc[["account_id", "is_mule", "ring_id", "typology", "role", "is_victim"]].copy()
    accounts = acc.drop(columns=["is_mule", "ring_id", "typology", "role", "is_victim"])
    return {"accounts": accounts, "devices": dev, "account_devices": ad, "transactions": tx,
            "complaints": comp, "ground_truth": truth}


def write(tables: dict[str, pd.DataFrame], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for name, df in tables.items():
        df.to_parquet(os.path.join(out_dir, f"{name}.parquet"), index=False)
