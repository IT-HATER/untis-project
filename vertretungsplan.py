import argparse
import datetime as dt
import html
import http.server
import os
import re
import sys
import threading
import time
from pathlib import Path

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
REFRESH_MIN = 5


# ------------------------------------------------------------------ config
def load_dotenv(path):
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Fehlt in .env: {name}")
    return v


def school_days(n):
    """Today + the next school days (skips Sat/Sun)."""
    d, out = dt.date.today(), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


# ------------------------------------------------------------------ fetching
FIELDS = ["id", "name"]


def hhmm(t):
    return f"{t // 100:02d}:{t % 100:02d}"


def to_date(d):
    return dt.date(d // 10000, d // 100 % 100, d % 100)


EXAM_WORDS = re.compile(r"schularbeit|test|prüfung|klausur|\bsa\b|\bsü\b|exam", re.I)


def raw_entry(p, kind):
    """Build an entry from a raw Untis period dict. The names come straight from
    Untis, so this works even without the right to call getTeachers()."""
    def get(key, field="name"):
        return [x[field] for x in p.get(key, []) if x.get(field) and x[field] != "---"]  # --- = no teacher
    return {
        "date": to_date(p["date"]),
        "start": hhmm(p["startTime"]),
        "end": hhmm(p["endTime"]),
        "klassen": get("kl") or ["?"],
        "subject": ", ".join(get("su")) or p.get("lstext") or p.get("substText") or "?",
        "teacher": ", ".join(sorted(get("te"))),
        "orig_teacher": ", ".join(sorted(get("te", "orgname"))),
        "room": ", ".join(get("ro")),
        "orig_room": ", ".join(get("ro", "orgname")),
        "kind": kind,  # "Entfall" | "Supplierung" | "Raumänderung" | "Verlegung" | "Zusatzstunde"
        "text": " ".join(t for t in (p.get("substText"), p.get("info"), p.get("lstext")) if t),
    }


SUBST_KIND = {"cancel": "Entfall", "subst": "Supplierung", "rmchg": "Raumänderung",
              "shift": "Verlegung", "add": "Zusatzstunde"}


def web_exams(s, klassen, days):
    """{period id: exam name} for every lesson WebUntis marks as an exam (Schularbeit,
    Test …). The old JSON-RPC timetable has no exam flag, so this asks the same
    web API the WebUntis website uses, with the session we are already logged in with."""
    import base64
    import requests

    base = f"https://{env('UNTIS_SERVER')}/WebUntis"
    school = base64.b64encode(env("UNTIS_SCHOOL").encode()).decode()
    cookies = {"JSESSIONID": s.config["jsessionid"], "schoolname": f'"_{school}"'}
    wanted = {int(d.strftime("%Y%m%d")) for d in days}
    weeks = sorted({d - dt.timedelta(days=d.weekday()) for d in days})
    out = {}
    for k in klassen:
        for monday in weeks:
            try:
                r = requests.get(f"{base}/api/public/timetable/weekly/data", cookies=cookies, timeout=20,
                                 params={"elementType": 1, "elementId": k.id,
                                         "date": monday.isoformat(), "formatId": 1})
                periods = r.json()["data"]["result"]["data"]["elementPeriods"].get(str(k.id), [])
            except Exception:
                continue  # web API not reachable / not allowed for this class
            for p in periods:
                if p.get("date") in wanted and (p.get("is", {}).get("exam") or p.get("cellState") == "EXAM"):
                    exam = p.get("exam") or {}
                    out[p["id"]] = p.get("lessonText") or f"Schularbeit {exam.get('name', '')}".strip()
    return out


def fetch(days):
    import webuntis

    s = webuntis.Session(server=env("UNTIS_SERVER"), school=env("UNTIS_SCHOOL"),
                         username=env("UNTIS_USER"), password=env("UNTIS_PASSWORD"),
                         useragent="vertretungsplan").login()
    try:
        start, end = days[0].strftime("%Y%m%d"), days[-1].strftime("%Y%m%d")
        # 1) Best source: the official substitution list (teacher/admin rights)
        try:
            raw = s._request("getSubstitutions", {"startDate": start, "endDate": end, "departmentId": 0})
            entries = [raw_entry(p, SUBST_KIND.get(p.get("type"), p.get("type"))) for p in raw]
            source = "Vertretungsliste"
        except Exception as e:
            print(f"Vertretungsliste nicht erlaubt ({e}) – lese Stundenpläne aller Klassen …")
            entries, source, seen = [], "Klassen-Stundenpläne", set()
            klassen = list(s.klassen())
            exams = web_exams(s, klassen, days)
            for k in klassen:
                try:
                    periods = s._request("getTimetable", {"options": {
                        "element": {"id": k.id, "type": 1}, "startDate": start, "endDate": end,
                        "klasseFields": FIELDS, "teacherFields": FIELDS, "subjectFields": FIELDS,
                        "roomFields": FIELDS, "showSubstText": True, "showInfo": True}})
                except Exception:
                    continue  # no right to see this class
                for p in periods:
                    if p["id"] in seen:  # same lesson, already read via another class
                        continue
                    seen.add(p["id"])
                    code = p.get("code")
                    if p["id"] in exams and code != "cancelled":
                        e = raw_entry(p, "Schularbeit")
                        e["text"] = " ".join(t for t in (exams[p["id"]], e["text"]) if t)
                        entries.append(e)
                    elif code == "cancelled":
                        entries.append(raw_entry(p, "Entfall"))
                    elif code == "irregular" or any("orgid" in t for t in p.get("te", [])):
                        e = raw_entry(p, "Supplierung")
                        if code == "irregular" and EXAM_WORDS.search(e["text"]):
                            e["kind"] = "Schularbeit"  # exam that takes the place of a lesson
                        entries.append(e)
        # lesson numbers (1., 2., …) from the school's time grid
        try:
            grid = {}
            for day in s.timegrid_units():
                for unit in day.time_units:
                    grid[unit.start.strftime("%H:%M")] = unit.name
        except Exception:
            grid = {}
    finally:
        s.logout()

    for e in entries:
        e["period"] = grid.get(e["start"], "")
    # only show what the user asked for: supplierte + entfallene Stunden
    entries = [e for e in entries if e["kind"] in ("Entfall", "Supplierung", "Schularbeit")]
    entries = merge_same_lesson(only_tests_that_cancel(drop_covered_entfall(entries)))
    entries.sort(key=sort_key)
    entries = join_consecutive(entries)
    return entries, source


def drop_covered_entfall(entries):
    """A cancelled lesson that is replaced by a Supplierung or a Schularbeit (same day,
    class and overlapping time) is not really an Entfall - drop it and note on the
    replacing lesson what it replaces (e.g. "statt D · RAMM")."""
    subst = [e for e in entries if e["kind"] in ("Supplierung", "Schularbeit")]

    def covered(e, k):
        for o in subst:
            if (o["date"] == e["date"] and k in o["klassen"]
                    and o["start"] < e["end"] and o["end"] > e["start"]):
                if not o.get("orig_subject") and e["subject"] != o["subject"]:
                    o["orig_subject"] = e["subject"]
                if not o["orig_teacher"]:
                    o["orig_teacher"] = e["orig_teacher"] or e["teacher"]
                o["replaces_lesson"] = True
                return True
        return False

    out = []
    for e in entries:
        if e["kind"] == "Entfall":
            left = [k for k in e["klassen"] if not covered(e, k)]
            if not left:
                continue
            e = {**e, "klassen": left}
        out.append(e)
    return out


def only_tests_that_cancel(entries):
    """Show a Schularbeit/Test only if a lesson is lost for it: a cancelled lesson of the
    class at that time, or the hour was taken from another teacher (Untis "statt …").
    A test written in the subject's own lesson is no change, so it is left out."""
    return [e for e in entries if e["kind"] != "Schularbeit"
            or e.get("replaces_lesson") or e["orig_teacher"]]


def class_key(k):
    """'8A' -> (8, 'a'): 1st grade first, then 2nd …; within a grade A, B, C …"""
    m = re.match(r"(\d+)(.*)", k)
    return (int(m.group(1)), m.group(2).lower()) if m else (99, k.lower())


def sort_key(e):
    return (e["date"], class_key(e["klassen"][0]), e["start"])


def join_consecutive(entries):
    """Hours in a row that are the same change become one row, e.g. "1.–5.  08:15–13:00".
    Same day, classes, kind, subject, room and info, only a break in between, and at least
    one teacher in common; teachers and what was replaced are put together.
    Expects entries sorted by sort_key."""
    def minutes(t):
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    def parts(x):
        return [t for t in (x or "").split(", ") if t]

    def union(a, b):
        return ", ".join(dict.fromkeys(parts(a) + parts(b)))

    out, last = [], {}
    for e in entries:
        key = (e["date"], tuple(e["klassen"]), e["kind"], e["subject"], e["room"], e["text"])
        prev = last.get(key)
        same_people = set(parts(prev["teacher"])) & set(parts(e["teacher"])) if prev else set()
        if (prev and 0 <= minutes(e["start"]) - minutes(prev["end"]) <= 20  # only a break between
                and (same_people or prev["teacher"] == e["teacher"])):
            prev["end"] = e["end"]
            prev["period_end"] = e["period"]
            prev["teacher"] = union(prev["teacher"], e["teacher"])
            prev["orig_teacher"] = union(prev["orig_teacher"], e["orig_teacher"])
            prev["orig_subject"] = union(prev.get("orig_subject"), e.get("orig_subject"))
            continue
        e = dict(e)
        last[key] = e
        out.append(e)
    return out


def merge_same_lesson(entries):
    """One row per lesson: if the same lesson (day, time, subject, teacher, kind) is
    listed for several classes, put all those classes into one row, lowest grade first."""
    rows = {}
    for e in entries:
        key = (e["date"], e["start"], e["kind"], e["subject"], e["teacher"], e["orig_teacher"])
        if key in rows:
            rows[key]["klassen"] += [k for k in e["klassen"] if k not in rows[key]["klassen"]]
        else:
            rows[key] = {**e, "klassen": list(e["klassen"])}
    for e in rows.values():
        e["klassen"].sort(key=class_key)
    return list(rows.values())


def demo(days):
    import random
    random.seed(4)
    klassen = ["1A", "1B", "2A", "2B", "3A", "3C", "4A", "4B", "5HB", "6A", "7B", "8A"]
    subj = ["M", "D", "E", "GW", "BIU", "PH", "CH", "GSP", "BSP", "INF", "L", "F"]
    teach = ["HUB", "MAY", "LEE", "KAI", "WOL", "STE", "BRA", "FIS", "GRU", "PIC"]
    times = [("07:50", "08:40", "1"), ("08:45", "09:35", "2"), ("09:50", "10:40", "3"),
             ("10:45", "11:35", "4"), ("11:40", "12:30", "5"), ("12:35", "13:25", "6")]
    out = []
    for d in days:
        for _ in range(random.randint(8, 14)):
            st, en, nr = random.choice(times)
            kind = random.choice(["Entfall", "Supplierung", "Supplierung"])
            o, t = random.sample(teach, 2)
            out.append({"date": d, "start": st, "end": en, "period": nr,
                        "klassen": [random.choice(klassen)], "subject": random.choice(subj),
                        "teacher": "" if kind == "Entfall" else t, "orig_teacher": o,
                        "room": f"{random.choice('ABC')}{random.randint(1, 20)}", "orig_room": "",
                        "kind": kind,
                        "text": random.choice(["", "", "", "Arbeitsauftrag auf Moodle", "mit 2B zusammen"])})
        # Untis often lists a substituted lesson twice: cancelled + new lesson
        sub = next((e for e in out if e["date"] == d and e["kind"] == "Supplierung"), None)
        if sub:
            out.append({**sub, "kind": "Entfall", "teacher": "", "text": ""})
        # a Schularbeit that replaces a cancelled lesson -> only the exam shows
        out.append({"date": d, "start": "08:45", "end": "09:35", "period": "2", "klassen": ["8A"],
                    "subject": "D", "teacher": "", "orig_teacher": "RAMM", "room": "R8A",
                    "orig_room": "", "kind": "Entfall", "text": ""})
        out.append({**out[-1], "subject": "E", "teacher": "SCHU", "orig_teacher": "",
                    "kind": "Schularbeit", "text": "2. Schularbeit Englisch"})
        # one cancelled lesson that three classes share -> must show as one row
        for k in ("2A", "4B", "2B"):
            out.append({"date": d, "start": "11:40", "end": "12:30", "period": "5",
                        "klassen": [k], "subject": "BSP", "teacher": "", "orig_teacher": "HUB",
                        "room": "TH1", "orig_room": "", "kind": "Entfall", "text": ""})
    out = merge_same_lesson(drop_covered_entfall(out))
    out.sort(key=sort_key)
    out = join_consecutive(out)
    return out, "Demo-Daten"


# ------------------------------------------------------------------ output: terminal
def print_table(entries, days):
    for d in days:
        rows = [e for e in entries if e["date"] == d]
        print(f"\n=== {WEEKDAYS[d.weekday()]}, {d:%d.%m.%Y} — {len(rows)} Änderung(en) ===")
        if not rows:
            print("  keine Supplierungen oder Entfälle")
            continue
        print(f"  {'Klasse':<14}{'Std':<5}{'Zeit':<13}{'Fach':<7}{'Art':<13}{'Lehrer':<16}{'Raum':<8}Info")
        for e in rows:
            lehrer = (f"{e['orig_teacher']} → {e['teacher']}" if e["kind"] != "Entfall"
                      else f"({e['orig_teacher'] or e['teacher']})")
            std = f"{e['period']}-{e['period_end']}" if e.get("period_end") else e["period"]
            print(f"  {', '.join(e['klassen']):<13} {std:<5}{e['start']}-{e['end']:<7}"
                  f"{e['subject']:<7}{e['kind']:<13}{lehrer:<16}{e['room']:<8}{e['text']}")


# ------------------------------------------------------------------ output: web page
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember"]

PAGE_CSS = """
:root{
  --bg:#f2f2f7; --card:#fff; --label:#000; --label2:rgba(60,60,67,.62); --label3:rgba(60,60,67,.36);
  --sep:rgba(60,60,67,.18); --bar:rgba(242,242,247,.78);
  --red:#d70015; --orange:#c93400; --blue:#0040dd;
  color-scheme:light;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{background:var(--bg);color:var(--label);
  font:1rem/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
  -webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}

/* 16:9 board for a TV / monitor: fixed frame, no scrolling, text scales to fit */
html,body{height:100%;overflow:hidden}
*{cursor:none!important}
body{display:flex;align-items:center;justify-content:center}
.stage{width:min(100vw,calc(100vh * 16 / 9));aspect-ratio:16/9;display:flex;flex-direction:column;
  background:var(--bg);overflow:hidden}
.bar{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;
  padding:.8rem 1.5rem;border-bottom:.5px solid var(--sep);flex:none}
.bar h1{font-size:1.25rem;font-weight:600;letter-spacing:-.01em}
.bar p{font-size:.875rem;color:var(--label2);white-space:nowrap}
.live{display:inline-block;width:.45rem;height:.45rem;border-radius:50%;background:#34c759;
  margin-right:.4rem;vertical-align:.08em}

main{flex:1;min-height:0;display:flex;gap:2rem;padding:1.1rem 1.5rem .6rem}
.day{flex:1 1 0;display:flex;flex-direction:column;min-width:0;min-height:0}
.day+.day{border-left:.5px solid var(--sep);padding-left:2rem}
.groups{flex:1;min-height:0;overflow:hidden}

.day h2{font-size:1.875rem;line-height:1.1;font-weight:700;letter-spacing:-.022em}
.day .date{display:block;font-size:.9375rem;font-weight:400;letter-spacing:0;color:var(--label2);margin-top:.2rem}
.day .sum{font-size:.9375rem;color:var(--label2);margin:.3rem 0 .9rem}
.day .sum b{font-weight:600}
.day .sum .x{color:var(--red)} .day .sum .s{color:var(--orange)}

.list{list-style:none;background:var(--card);overflow:hidden}
/* one line per lesson: 1.  08:15  D  Entfällt · BURZ  (note)  R9B */
.row{display:flex;align-items:baseline;gap:.6rem;padding:.4rem .8rem;position:relative;white-space:nowrap}
.row+.row::before{content:"";position:absolute;top:0;left:calc(var(--kw) * 1ch + 1.4rem);right:0;border-top:.5px solid var(--sep)}
.row.first+.row.first::before,.row+.row.first::before{left:0;border-top:1px solid var(--label3)}
.kl{flex:none;width:calc(var(--kw) * 1ch);font-size:1rem;font-weight:700;white-space:normal;line-height:1.2}
.nr{flex:none;width:3.2rem;text-align:right;font-size:1rem;font-weight:600}
.time{flex:none;width:4.4rem;font-size:.75rem;color:var(--label2)}
.what{flex:1;min-width:0;display:flex;align-items:baseline;overflow:hidden}
.subj{flex:none;font-size:1rem;font-weight:600;margin-right:.5rem}
.x .subj{color:var(--label2);text-decoration:line-through;text-decoration-thickness:1px}
.status{flex:none;font-size:.875rem;color:var(--label2)}
.status .k{font-weight:600}
.x .status .k{color:var(--red)} .s .status .k{color:var(--orange)} .e .status .k{color:var(--blue)}
.day .sum .e{color:var(--blue)}
.status .new{color:var(--label);font-weight:600}
.note{flex:1 1 0;min-width:0;overflow:hidden;text-overflow:ellipsis;font-size:.75rem;color:var(--label2);margin-left:.5rem}
.note::before{content:"· "}
.room{flex:none;font-size:.875rem;color:var(--label2)}

.empty{background:var(--card);padding:1.5rem 1rem;text-align:center;
  color:var(--label2);font-size:.9375rem}
footer{flex:none;padding:.3rem 1.5rem .6rem;font-size:.75rem;color:var(--label3)}

@media (prefers-contrast:more){:root{--label2:var(--label);--sep:var(--label3)}}
"""


# Runs in the page: 1) scale text with the 16:9 frame (1920 px wide -> 18 px) and shrink
# it until each day fits - but never below a readable size (1920 px -> 15 px);
# 2) a day that still does not fit scrolls slowly down, waits, and jumps back up;
# 3) asks the server every 20 s whether it has new data and swaps it in without reloading.
PAGE_JS = """
var MIN_DIV=125, SPEED=28, WAIT_TOP=5000, WAIT_BOTTOM=3000, run=0;
function fit(){
  var stage=document.querySelector('.stage'), root=document.documentElement;
  var size=stage.clientWidth/106, min=stage.clientWidth/MIN_DIV;
  function over(){
    return [].some.call(document.querySelectorAll('.groups'),function(g){
      return g.scrollWidth>g.clientWidth+1||g.scrollHeight>g.clientHeight+1;})
      || [].some.call(document.querySelectorAll('.what'),function(w){return w.scrollWidth>w.clientWidth+1;});
  }
  root.style.fontSize=size+'px';
  while(over()&&size>min){size*=0.97;root.style.fontSize=Math.max(size,min)+'px';}
  autoscroll();
}
function autoscroll(){
  var me=++run;  // a newer call stops the older scroll loops
  [].forEach.call(document.querySelectorAll('.groups'),function(g){
    g.scrollTop=0;
    if(g.scrollHeight<=g.clientHeight+1) return;
    var pos=0, last=null, waitUntil=performance.now()+WAIT_TOP;
    function step(t){
      if(me!==run) return;
      var max=g.scrollHeight-g.clientHeight;
      if(t<waitUntil){last=t;requestAnimationFrame(step);return;}
      if(last===null) last=t;
      pos+=SPEED*(t-last)/1000; last=t;
      if(pos>=max){g.scrollTop=max;pos=0;
        setTimeout(function(){if(me===run){g.scrollTop=0;waitUntil=performance.now()+WAIT_TOP;last=null;}},WAIT_BOTTOM);
        waitUntil=performance.now()+WAIT_BOTTOM+10;}
      else g.scrollTop=pos;
      requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  });
}
function build(){var m=document.querySelector('meta[name=build]');return m&&m.content;}
function poll(){
  if(location.protocol==='file:') return;
  fetch('/version',{cache:'no-store'}).then(function(r){return r.ok?r.text():null;}).then(function(v){
    if(!v||v===build()) return;
    return fetch('/',{cache:'no-store'}).then(function(r){return r.text();}).then(function(t){
      var doc=new DOMParser().parseFromString(t,'text/html');
      document.querySelector('.stage').innerHTML=doc.querySelector('.stage').innerHTML;
      document.querySelector('meta[name=build]').content=v;
      fit();
    });
  }).catch(function(){});
}
addEventListener('load',fit);addEventListener('resize',fit);fit();
setInterval(poll,20000);
"""


def render_html(entries, days, source, live=False, stamp=""):
    esc = html.escape
    now = dt.datetime.now()

    def plural(n, one, many):
        return f"{n} {one if n == 1 else many}"

    sections = []
    for d in days:
        rows = [e for e in entries if e["date"] == d]
        n_x = sum(e["kind"] == "Entfall" for e in rows)
        n_e = sum(e["kind"] == "Schularbeit" for e in rows)
        n_s = len(rows) - n_x - n_e

        groups = {}
        for e in rows:
            groups.setdefault(", ".join(e["klassen"]), []).append(e)

        lis = []
        for klasse, items in groups.items():
            for i, e in enumerate(items):
                if e["kind"] == "Entfall":
                    cls = "x"
                    status = '<span class="k">Entfällt</span>'
                    if e["orig_teacher"] or e["teacher"]:
                        status += f' · {esc(e["orig_teacher"] or e["teacher"])}'
                else:
                    exam = e["kind"] == "Schularbeit"
                    cls = "e" if exam else "s"
                    status = (f'<span class="k">{"Schularbeit/Test" if exam else "Supplierung"}</span> · '
                              f'<span class="new">{esc(e["teacher"] or "?")}</span>')
                    was = " · ".join(x for x in (e.get("orig_subject"), e["orig_teacher"]) if x)
                    if was:
                        status += f' statt {esc(was)}'
                nr = f'{esc(e["period"])}.' if e["period"] else "–"
                when = e["start"]
                if e.get("period_end"):  # several hours in a row
                    nr = f'{esc(e["period"])}.–{esc(e["period_end"])}.'
                    when = f'{e["start"]}–{e["end"]}'
                note = f'<span class="note">{esc(e["text"])}</span>' if e["text"] else ""
                first = " first" if i == 0 else ""
                lis.append(
                    f'<li class="row {cls}{first}"><span class="kl">{esc(klasse) if i == 0 else ""}</span>'
                    f'<span class="nr">{nr}</span><span class="time">{when}</span>'
                    f'<span class="what"><span class="subj">{esc(e["subject"])}</span>'
                    f'<span class="status">{status}</span>{note}</span>'
                    f'<span class="room">{esc(e["room"])}</span></li>')

        if rows:
            summary = (f'<p class="sum"><b class="x">{plural(n_x, "Entfall", "Entfälle")}</b> · '
                       f'<b class="s">{plural(n_s, "Supplierung", "Supplierungen")}</b>'
                       + (f' · <b class="e">{plural(n_e, "Schularbeit/Test", "Schularbeiten/Tests")}</b>' if n_e else "")
                       + '</p>')
            kw = min(max(len(k) for k in groups), 22)  # width of the class column
            content = f'<ul class="list" style="--kw:{kw}">{"".join(lis)}</ul>'
        else:
            summary = '<p class="sum">&nbsp;</p>'
            content = '<p class="empty">Keine Änderungen</p>'

        label = "Heute" if d == now.date() else ("Morgen" if d == now.date() + dt.timedelta(days=1)
                                                 else WEEKDAYS[d.weekday()])
        date_txt = f"{WEEKDAYS[d.weekday()]}, {d.day}. {MONTHS[d.month - 1]}"
        sections.append(f'<section class="day"><h2>{label}<span class="date">{date_txt}</span></h2>'
                        f'{summary}<div class="groups">{content}</div></section>')

    # served page updates itself via PAGE_JS; a saved .html file just reloads now and then
    meta = (f'<meta name="build" content="{esc(stamp)}">' if live
            else f'<meta http-equiv="refresh" content="{REFRESH_MIN * 60}">')
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">{meta}
<title>Vertretungsplan</title><style>{PAGE_CSS}</style></head><body><div class="stage">
<header class="bar"><h1>Vertretungsplan</h1>
<p><span class="live"></span>Aktualisiert {now:%H:%M}</p></header>
<main>{''.join(sections)}</main>
<footer>Quelle: WebUntis ({esc(source)}) · lädt alle {REFRESH_MIN:g} Minuten neu</footer>
</div><script>{PAGE_JS}</script></body></html>"""


# ------------------------------------------------------------------ main
def build(args):
    days = school_days(args.days)
    entries, source = demo(days) if args.demo else fetch(days)
    return entries, days, source


def main():
    ap = argparse.ArgumentParser(description="Supplierungen und Entfälle aller Klassen")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--html", action="store_true")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--refresh", type=float, default=5, help="Minuten zwischen Aktualisierungen")
    args = ap.parse_args()
    global REFRESH_MIN
    REFRESH_MIN = args.refresh
    load_dotenv(Path(__file__).with_name(".env"))

    entries, days, source = build(args)
    print_table(entries, days)
    stamp = str(time.time())
    page = {"html": render_html(entries, days, source, live=args.serve, stamp=stamp), "stamp": stamp}

    if args.html:
        out = Path(__file__).with_name("vertretungsplan.html")
        out.write_text(page["html"], encoding="utf-8")
        print(f"\nHTML geschrieben: {out}")

    if args.serve:
        def refresher():
            while True:
                time.sleep(REFRESH_MIN * 60)
                try:
                    stamp = str(time.time())
                    page["html"] = render_html(*build(args), live=True, stamp=stamp)
                    page["stamp"] = stamp  # browsers see the new stamp and swap in the new data
                except Exception as e:
                    print("Aktualisierung fehlgeschlagen:", e)
        threading.Thread(target=refresher, daemon=True).start()

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/version"):
                    data, ctype = page["stamp"].encode(), "text/plain"
                else:
                    data, ctype = page["html"].encode("utf-8"), "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        print(f"\nLäuft auf http://localhost:{args.port}  (im Schulnetz: http://<IP dieses PCs>:{args.port})")
        http.server.ThreadingHTTPServer(("0.0.0.0", args.port), H).serve_forever()


if __name__ == "__main__":
    main()
