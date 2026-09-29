#!/usr/bin/env python3
"""
Banking EOD Reconciliation - ZOAU + Python demo
-----------------------------------------------
Runs in z/OS UNIX (USS) with IBM Z Open Automation Utilities (ZOAU)
installed and its Python bindings (zoautil_py) importable.

What it does:
  1. Seeds TEST datasets: opening balances + today's transactions
  2. Reads them into Python, posts every transaction, computes closing balances
  3. Flags exceptions: overdrafts, large-value txns, duplicate refs, unknown accounts
  4. Writes closing balances + exceptions report back to z/OS datasets
  5. Submits a JCL job that prints the exceptions report to spool,
     waits for it, and reads the spool back into Python

Usage:
  python3 bank_eod_demo.py --hlq MYUSER            # seed + run
  python3 bank_eod_demo.py --hlq MYUSER --cleanup  # delete demo datasets

SAFETY: point this at TEST datasets only. Every run deletes and recreates
<HLQ>.BANKDEMO.*. Never use a production HLQ.

Note: zoautil_py signatures differ slightly between ZOAU versions
(e.g. datasets.create uses `dataset_type` in 1.3, `type` in 1.2).
Check `help(datasets.create)` on your system if a call fails.
"""
import argparse
import sys
import time
from decimal import Decimal

from zoautil_py import datasets, jobs

LARGE_TXN_LIMIT = Decimal("1000000.00")   # flag single txns at/above this
JOB_TIMEOUT_SECS = 120

# ---------------- Record layouts (FB, LRECL=80) ----------------
# BALS: acct(0-10) sign(10) amount-in-paise(11-24)
# TXNS: ref(0-12) acct(12-22) type C/D(22) amount-in-paise(23-36) timestamp(36-50)

SEED_BALANCES = [
    ("SB00000001", Decimal("50000.00")),
    ("SB00000002", Decimal("1200.00")),
    ("CA00000003", Decimal("2500000.00")),
    ("SB00000004", Decimal("0.00")),
]

SEED_TXNS = [
    ("TXN000000001", "SB00000001", "D", Decimal("15000.00"),   "20260929093012"),
    ("TXN000000002", "SB00000002", "D", Decimal("5000.00"),    "20260929101544"),  # -> overdraft
    ("TXN000000003", "CA00000003", "C", Decimal("1500000.00"), "20260929113001"),  # -> large value
    ("TXN000000004", "SB00000004", "C", Decimal("25000.00"),   "20260929120000"),
    ("TXN000000004", "SB00000004", "C", Decimal("25000.00"),   "20260929120003"),  # -> duplicate
    ("TXN000000005", "SB00000099", "C", Decimal("700.00"),     "20260929143210"),  # -> unknown acct
    ("TXN000000006", "SB00000001", "C", Decimal("2000.00"),    "20260929160045"),
]


# ---------------- Fixed-width encode / decode ----------------
def paise(amount):
    return int((amount * 100).to_integral_value())


def fmt_balance(acct, amount):
    sign = "-" if amount < 0 else "+"
    return f"{acct:<10}{sign}{paise(abs(amount)):013d}".ljust(80)


def parse_balance(rec):
    sign = -1 if rec[10] == "-" else 1
    return rec[0:10].strip(), sign * Decimal(int(rec[11:24])) / 100


def fmt_txn(ref, acct, typ, amount, ts):
    return f"{ref:<12}{acct:<10}{typ}{paise(amount):013d}{ts:<14}".ljust(80)


def parse_txn(rec):
    return {
        "ref": rec[0:12].strip(),
        "acct": rec[12:22].strip(),
        "type": rec[22],
        "amount": Decimal(int(rec[23:36])) / 100,
        "ts": rec[36:50].strip(),
    }


# ---------------- ZOAU dataset helpers ----------------
def dsn(hlq, name):
    return f"{hlq}.BANKDEMO.{name}"


def recreate(name, lines):
    """Delete (if present) and allocate a fresh FB/80 sequential dataset."""
    if datasets.exists(name):
        datasets.delete(name)
    datasets.create(name, dataset_type="SEQ", record_format="FB", record_length=80)
    if lines:
        datasets.write(name, "\n".join(lines))


def read_records(name):
    # ZOAU hands back text already converted from EBCDIC.
    # Re-pad to 80 because trailing blanks can be trimmed on read.
    text = datasets.read(name)
    return [r.ljust(80) for r in text.splitlines() if r.strip()]


# ---------------- Business logic ----------------
def reconcile(balances, txns):
    closing = dict(balances)
    exceptions, seen = [], set()

    for t in txns:
        if t["ref"] in seen:
            exceptions.append(("DUPLICATE", t["ref"], t["acct"], t["amount"], "duplicate ref - skipped"))
            continue
        seen.add(t["ref"])

        if t["acct"] not in closing:
            exceptions.append(("UNKNOWN_ACCT", t["ref"], t["acct"], t["amount"], "unknown acct - not posted"))
            continue

        closing[t["acct"]] += t["amount"] if t["type"] == "C" else -t["amount"]

        if t["amount"] >= LARGE_TXN_LIMIT:
            exceptions.append(("LARGE_VALUE", t["ref"], t["acct"], t["amount"], "at/above reporting limit"))

    for acct, bal in closing.items():
        if bal < 0:
            exceptions.append(("OVERDRAFT", "-", acct, bal, "closing balance negative"))

    return closing, exceptions


def fmt_exception(code, ref, acct, amount, note):
    return f"{code:<13}{ref:<13}{acct:<11}{amount:>15,.2f} {note}"[:80]


def build_jcl(except_dsn):
    # Job card fields (account, CLASS, MSGCLASS) are site-specific - adjust.
    return "\n".join([
        "//BANKRPT  JOB (ACCT),'EOD EXCEPTIONS',CLASS=A,MSGCLASS=H,",
        "//             MSGLEVEL=(1,1),NOTIFY=&SYSUID",
        "//PRINT    EXEC PGM=IEBGENER",
        "//SYSPRINT DD SYSOUT=*",
        "//SYSIN    DD DUMMY",
        f"//SYSUT1   DD DSN={except_dsn},DISP=SHR",
        "//SYSUT2   DD SYSOUT=*",
    ])


# Status strings vary slightly by ZOAU version; compare with `jls` output.
TERMINAL_STATUSES = ("CC", "ABEND", "JCLERR", "CANCEL", "SEC", "CONV")


def wait_for(job, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job.refresh()
        if str(job.status).upper().startswith(TERMINAL_STATUSES):
            return job
        time.sleep(2)
    raise TimeoutError(f"Job {job.job_id} still running after {timeout}s")


# ---------------- Main ----------------
def main():
    ap = argparse.ArgumentParser(description="ZOAU banking EOD reconciliation demo")
    ap.add_argument("--hlq", required=True, help="High-level qualifier for TEST datasets")
    ap.add_argument("--cleanup", action="store_true", help="Delete demo datasets and exit")
    args = ap.parse_args()

    hlq = args.hlq.upper()
    names = {k: dsn(hlq, k) for k in ("BALS", "TXNS", "CLOSE", "EXCEPT", "JCL")}

    if args.cleanup:
        for n in names.values():
            if datasets.exists(n):
                datasets.delete(n)
                print(f"deleted {n}")
        return 0

    print("[1] Seeding test datasets")
    recreate(names["BALS"], [fmt_balance(a, b) for a, b in SEED_BALANCES])
    recreate(names["TXNS"], [fmt_txn(*t) for t in SEED_TXNS])

    print("[2] Reading balances and transactions from z/OS")
    balances = dict(parse_balance(r) for r in read_records(names["BALS"]))
    txns = [parse_txn(r) for r in read_records(names["TXNS"])]
    print(f"    {len(balances)} accounts, {len(txns)} transactions")

    print("[3] Posting transactions and checking exceptions")
    closing, exceptions = reconcile(balances, txns)

    print("[4] Writing closing balances and exceptions report")
    recreate(names["CLOSE"], [fmt_balance(a, b) for a, b in sorted(closing.items())])
    report = [f"EOD EXCEPTIONS {time.strftime('%Y-%m-%d')}  COUNT={len(exceptions)}"]
    report += [fmt_exception(*e) for e in exceptions]
    recreate(names["EXCEPT"], report)

    print("[5] Submitting report job and waiting for completion")
    recreate(names["JCL"], build_jcl(names["EXCEPT"]).splitlines())
    job = jobs.submit(names["JCL"])
    print(f"    submitted {job.job_id}")
    wait_for(job, JOB_TIMEOUT_SECS)
    rc = str(job.return_code)
    print(f"    status={job.status} rc={rc}")

    spool = jobs.read_output(job.job_id, "PRINT", "SYSUT2")

    print("\n=== CLOSING BALANCES ===")
    for acct, bal in sorted(closing.items()):
        print(f"  {acct}  {balances[acct]:>15,.2f}  ->  {bal:>15,.2f}")

    print("\n=== EXCEPTIONS (read back from job spool) ===")
    print(spool.rstrip())

    # Treat "0", "0000" or "CC 0000" as success
    return 0 if rc.replace("CC", "").strip().lstrip("0") == "" else 1


if __name__ == "__main__":
    sys.exit(main())
