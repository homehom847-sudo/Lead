"""SUSU Real Estate AI - Flask backend (matches templates/index.html).
Run locally:  pip install -r requirements.txt && python app.py
Deploy:       gunicorn app:app
"""
import json, os, random, re, threading, urllib.request, uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from flask import Flask, jsonify, render_template, request, Response

app = Flask(__name__)
now = lambda: datetime.now(timezone.utc)

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
KEYS_FILE = os.path.join(DATA_DIR, "keys.json")
LOCK = threading.Lock()

PROVIDERS = [
    {"id": "tavily", "name": "Tavily", "group": "search", "model": "", "about": "Live web search for leads and listings"},
    {"id": "gemini", "name": "Gemini", "group": "ai", "model": "gemini-2.5-flash", "about": "Understands requests better"},
]
PBY = {p["id"]: p for p in PROVIDERS}
ENV = {"tavily": "TAVILY_API_KEY", "gemini": "GEMINI_API_KEY"}


# ---------- optional password for the whole site ----------
@app.before_request
def gate():
    pw = os.getenv("SUSU_PASSWORD")
    if not pw:
        return
    a = request.authorization
    if not a or a.password != pw or a.username != os.getenv("SUSU_USER", "susu"):
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="SUSU"'})


# ---------- key storage ----------
def load_keys():
    try:
        with open(KEYS_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except Exception:
        return []


def save_keys(ks):
    try:
        with open(KEYS_FILE, "w") as f:
            json.dump(ks, f)
    except OSError:
        pass  # read-only disk: keys then live only until restart


KEYS = load_keys()


def active(group):
    """Return (secret, info) for the enabled key of a group, else the env var, else (None, None)."""
    for k in KEYS:
        if k["group"] == group and k["enabled"]:
            return k["key"], {"label": PBY[k["provider"]]["name"], "name": k["name"], "env": False, "model": k.get("model", "")}
    for p in PROVIDERS:
        if p["group"] == group and os.getenv(ENV[p["id"]]):
            return os.getenv(ENV[p["id"]]), {"label": p["name"], "name": ".env", "env": True, "model": os.getenv("GEMINI_MODEL", "")}
    return None, None


def keys_payload():
    pub = [{"id": k["id"], "name": k["name"], "provider": k["provider"], "group": k["group"],
            "label": PBY[k["provider"]]["name"], "model": k.get("model", ""), "enabled": k["enabled"],
            "hint": "••••" + k["key"][-4:]} for k in KEYS]
    using = {g: active(g)[1] for g in ("search", "ai") if active(g)[1]}
    return {"keys": pub, "providers": PROVIDERS, "using": using, "live": bool(active("search")[0])}


def find_key(kid):
    return next((k for k in KEYS if k["id"] == kid), None)


def exclusive(k):
    for o in KEYS:
        if o["group"] == k["group"] and o is not k:
            o["enabled"] = False


@app.get("/api/config/status")
def cfg_status():
    return jsonify(live=bool(active("search")[0]))


@app.get("/api/keys")
def keys_list():
    return jsonify(keys_payload())


@app.post("/api/keys")
def keys_add():
    b = request.get_json(silent=True) or {}
    name, prov, key = (b.get("name") or "").strip()[:40], b.get("provider"), (b.get("key") or "").strip()
    if not name:
        return jsonify(error="Enter a name first."), 400
    if prov not in PBY:
        return jsonify(error="Choose a key type."), 400
    if not key:
        return jsonify(error="Paste the key first."), 400
    with LOCK:
        k = {"id": uuid.uuid4().hex[:10], "name": name, "provider": prov, "group": PBY[prov]["group"], "key": key,
             "model": (b.get("model") or "").strip() if PBY[prov]["group"] == "ai" else "", "enabled": True}
        exclusive(k)
        KEYS.append(k)
        save_keys(KEYS)
    return jsonify(keys_payload())


@app.patch("/api/keys/<kid>")
def keys_patch(kid):
    with LOCK:
        k = find_key(kid)
        if not k:
            return jsonify(error="Key not found."), 404
        k["enabled"] = bool((request.get_json(silent=True) or {}).get("enabled"))
        if k["enabled"]:
            exclusive(k)
        save_keys(KEYS)
    return jsonify(keys_payload())


@app.delete("/api/keys/<kid>")
def keys_del(kid):
    with LOCK:
        KEYS[:] = [k for k in KEYS if k["id"] != kid]
        save_keys(KEYS)
    return jsonify(keys_payload())


@app.post("/api/keys/<kid>/test")
def keys_test(kid):
    k = find_key(kid)
    if not k:
        return jsonify(error="Key not found."), 404
    try:
        if k["provider"] == "tavily":
            tavily("real estate Lagos", k["key"], max_results=1)
        else:
            gemini("Reply with OK", k["key"], k.get("model") or None)
        return jsonify(ok=True, message="Key works.")
    except Exception as e:
        return jsonify(ok=False, message=f"Test failed: {e}")


# ---------- outside calls ----------
def http(url, body, headers):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def tavily(q, key, **kw):
    return http("https://api.tavily.com/search", {"query": q, "max_results": 8, **kw}, {"Authorization": "Bearer " + key})


def gemini(prompt, key, model=None, as_json=False):
    model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if as_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    d = http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", body, {"x-goog-api-key": key})
    return d["candidates"][0]["content"]["parts"][0]["text"]


# ---------- understanding the request ----------
UNIT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}
AMT = r"(\d[\d,]*(?:\.\d+)?)\s*(billion|million|thousand|b|m|k)?\b(?!\s*-?\s*(?:bed|br))"
KINDS = ("apartment", "flat", "house", "duplex", "bungalow", "terrace", "land", "office", "shop", "warehouse")


def money(txt):
    m = re.search(r"(?:under|below|max(?:imum)?|within|budget(?:\s+(?:of|is|around))?|around|about|up to|₦|\bn(?=\d))\s*₦?\s*" + AMT, txt)
    if not m:
        return None
    v = float(m.group(1).replace(",", "").rstrip(".")) * UNIT.get(m.group(2) or "", 1)
    return int(v) if v >= 10000 else None


def parse_query(q):
    t = q.lower()
    b = re.search(r"(\d+)\s*-?\s*(?:bed|br)", t)
    loc = re.search(r"\b(?:in|around|at|near)\s+([a-z][a-z ]*?)(?=\s+(?:under|below|max|within|budget|for|with|around)\b|[,.?!]|$)", t)
    return {"beds": int(b.group(1)) if b else None, "budget": money(t),
            "loc": loc.group(1).strip().title() if loc else None,
            "kind": next((k for k in KINDS if k in t), None)}


def classify(q):
    t = q.lower()
    if re.search(r"\bmatch", t):
        return "match"
    if re.search(r"\b(buyers?|leads?|looking|need|needs|want|wants|wanted|anyone|searching|interested)\b", t):
        return "leads"
    return "properties"


def understand(q):
    """Built-in parser, refined by Gemini if an AI key is on."""
    p, intent = parse_query(q), classify(q)
    key, info = active("ai")
    if key:
        try:
            out = json.loads(gemini('Return only JSON {"intent":"leads"|"properties"|"match","beds":int|null,'
                                    '"budget":int NGN|null,"loc":str|null,"kind":str|null} for this real estate request: ' + q,
                                    key, info.get("model") or None, True))
            if out.get("intent") in ("leads", "properties", "match"):
                intent = out["intent"]
            p.update({a: out[a] for a in p if out.get(a)})
        except Exception:
            pass
    return intent, p


# ---------- helpers ----------
def age_hours(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        try:
            d = parsedate_to_datetime(str(s))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return max(0, (now() - d).total_seconds() / 3600)


def ago(h):
    return f"{max(1, round(h * 60))} min ago" if h < 1 else f"{round(h)} hours ago" if h < 24 else f"{round(h / 24)} days ago"


def public_contact(text):
    """Only contact details that literally appear in the public text."""
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text) or re.search(r"(?:\+234|\b0)[\s-]?[789]\d[\s-]?\d[\s-]?\d{3}[\s-]?\d{4}|\b0[789]\d{9}\b", text)
    return m.group(0) if m else None


def host(u):
    try:
        return urlparse(u).netloc.replace("www.", "") or "Web"
    except Exception:
        return "Web"


def uid(p):
    return f"{p}-{uuid.uuid4().hex[:8]}"


def score_lead(l):
    h = age_hours(l["posted_at"])
    rec = 0 if h is None else round(30 * max(0, 1 - h / 168))
    intent = bool(re.search(r"looking|need|want|searching|buy|wanted|anyone|interested", l["request"].lower()))
    R = [("Recency", "Posting date unavailable" if h is None else "Posted " + ago(h), rec, 30),
         ("Buying intent", "Clear buyer wording" if intent else "No clear buying words", 25 if intent else 0, 25),
         ("Location", l["location"] or "Not stated", 10 if l["location"] else 0, 10),
         ("Budget", "Stated" if l["budget"] else "Not stated", 15 if l["budget"] else 0, 15),
         ("Property type", l["type"] or "Not stated", 5 if l["type"] else 0, 5),
         ("Bedrooms", str(l["bedrooms"]) if l["bedrooms"] else "Not stated", 5 if l["bedrooms"] else 0, 5),
         ("Public contact", "Found in public text" if l["contact"] else "None found", 10 if l["contact"] else 0, 10)]
    l["reasons"] = [{"label": a, "note": b, "pts": c, "max": d} for a, b, c, d in R]
    l["score"] = min(97, max(5, sum(r["pts"] for r in l["reasons"])))
    l["missing"] = [k for k, v in (("location", l["location"]), ("budget", l["budget"]), ("type", l["type"]), ("bedrooms", l["bedrooms"])) if not v]
    return l


# ---------- demo data (clearly sample) ----------
NAMES = ["Amina B.", "Chuka O.", "Tunde A.", "Ngozi E.", "Ibrahim S.", "Funmi K.", "Emeka N.", "Zainab M."]
AREAS = ["Phase 2", "Central", "Estate Road", "Extension", "Garden City"]


def demo_leads(p, q):
    r, out = random.Random("l" + q), []
    loc = p["loc"] or "Lagos"
    for _ in range(6):
        kind = p["kind"] or r.choice(["apartment", "house", "land", "duplex"])
        beds = p["beds"] if p["beds"] or kind == "land" else r.choice([2, 3, 4])
        bud = int((p["budget"] or r.choice([5e6, 12e6, 25e6])) * r.uniform(.8, 1.15) // 5e5 * 5e5)
        bed_txt = f"{beds} bedroom " if beds and kind != "land" else ""
        txt = f"{r.choice(['Looking for', 'Need', 'Searching for'])} a {bed_txt}{kind} in {loc}, budget around ₦{bud:,}. " \
              f"{r.choice(['Please share leads.', 'Need to move soon.', 'Genuine sellers only.'])}"
        name = r.choice(NAMES) if r.random() > .25 else None
        out.append({"id": uid("l"), "name": name, "request": txt, "location": loc, "budget": bud, "type": kind,
                    "bedrooms": beds if kind != "land" else None,
                    "posted_at": (now() - timedelta(hours=r.choice([1, 3, 6, 14, 30, 60, 120, 200]))).isoformat(),
                    "contact": f"@{name.split()[0].lower()}_demo" if name and r.random() < .5 else None,
                    "source": "Demo data", "url": None})
    return out


def demo_props(p, q):
    r, out = random.Random("p" + q), []
    loc = p["loc"] or "Lagos"
    for _ in range(6):
        kind = p["kind"] or r.choice(["apartment", "house", "land", "duplex"])
        beds = p["beds"] if p["beds"] or kind == "land" else r.choice([2, 3, 4])
        title = f"{beds}-bedroom {kind}" if beds and kind != "land" else f"Plot of {kind}"
        out.append({"id": uid("p"), "title": title, "description": "Sample listing for demonstration.",
                    "price": int((p["budget"] or r.choice([5e6, 12e6, 25e6])) * r.uniform(.6, 1.1) // 5e5 * 5e5),
                    "location": f"{r.choice(AREAS)}, {loc}", "type": kind, "bedrooms": beds if kind != "land" else None,
                    "image": None, "contact": None, "source": "Demo data", "url": None,
                    "posted_at": (now() - timedelta(hours=r.choice([2, 8, 20, 48, 96]))).isoformat(), "missing": []})
    return out


DEMO_NOTE = "Demo mode: these are sample results, not real people or listings. Add a Tavily key under API keys to search the live web."


# ---------- live search ----------
def live_leads(q, p, key):
    query = f"looking to buy {p['kind'] or 'property'} {('in ' + p['loc']) if p['loc'] else ''} Nigeria budget wanted"
    out = []
    for x in tavily(query, key)["results"]:
        c = x.get("content", "")
        e = parse_query(c)
        out.append({"id": uid("l"), "name": None, "request": (x.get("title") or c)[:140] if len(c) < 20 else c[:200],
                    "location": e["loc"] or p["loc"], "budget": e["budget"] or p["budget"], "type": e["kind"] or p["kind"],
                    "bedrooms": e["beds"] or p["beds"], "posted_at": x.get("published_date"),
                    "contact": public_contact(c), "source": host(x["url"]), "url": x["url"]})
    return out


def live_props(q, p, key):
    d = tavily(f"{p['kind'] or 'property'} for sale or rent {('in ' + p['loc']) if p['loc'] else ''} Nigeria {q}", key, include_images=True)
    imgs = [i if isinstance(i, str) else i.get("url") for i in d.get("images", [])]
    out = []
    for i, x in enumerate(d["results"]):
        c = x.get("content", "")
        pr = re.search(r"₦\s*(\d[\d,]*(?:\.\d+)?)\s*(million|m|k|billion|b)?\b", c, re.I)
        price = int(float(pr.group(1).replace(",", "")) * UNIT.get((pr.group(2) or "").lower(), 1)) if pr else None
        e = parse_query(c)
        item = {"id": uid("p"), "title": (x.get("title") or "Listing")[:90], "description": c[:160], "price": price,
                "location": e["loc"] or p["loc"], "type": e["kind"] or p["kind"], "bedrooms": e["beds"],
                "image": imgs[i] if i < len(imgs) else None, "contact": public_contact(c), "source": host(x["url"]),
                "url": x["url"], "posted_at": x.get("published_date")}
        item["missing"] = [k for k, v in (("price", price), ("location", item["location"]), ("bedrooms", item["bedrooms"])) if not v]
        out.append(item)
    return out


def run_search(kind, q, p):
    """Returns (items, notes). Falls back to demo data if live search is off or fails."""
    key, _ = active("search")
    if key:
        try:
            return (live_leads if kind == "leads" else live_props)(q, p, key), []
        except Exception as e:
            fail = f"Live search failed ({e}). Showing demo data instead."
            return (demo_leads if kind == "leads" else demo_props)(p, q), [fail, DEMO_NOTE]
    return (demo_leads if kind == "leads" else demo_props)(p, q), [DEMO_NOTE]


# ---------- matching ----------
def match_pair(l, p):
    C, pts, known = [], 0, 0

    def add(label, status, detail, w):
        nonlocal pts, known
        C.append({"label": label, "status": status, "detail": detail})
        if status != "unknown":
            known += w
        pts += {"match": w, "close": w * .5}.get(status, 0)

    ll, pl = (l.get("location") or "").lower(), (p.get("location") or "").lower()
    if not ll or not pl:
        add("Location", "unknown", "Not stated", 30)
    elif ll in pl or pl in ll:
        add("Location", "match", f"Both in {l['location']}", 30)
    else:
        add("Location", "miss", f"{l['location']} vs {p['location']}", 30)
    b, pr = l.get("budget"), p.get("price")
    if not b or not pr:
        add("Budget", "unknown", "Price or budget not stated", 30)
    elif pr <= b:
        add("Budget", "match", f"₦{pr:,} is within ₦{b:,}", 30)
    elif pr <= b * 1.1:
        add("Budget", "close", f"₦{pr:,} is up to 10% over ₦{b:,}", 30)
    else:
        add("Budget", "miss", f"₦{pr:,} is over ₦{b:,}", 30)
    lb, pb = l.get("bedrooms"), p.get("bedrooms")
    if not lb or not pb:
        add("Bedrooms", "unknown", "Not stated", 20)
    elif lb == pb:
        add("Bedrooms", "match", f"Both {pb}", 20)
    elif abs(lb - pb) == 1:
        add("Bedrooms", "close", f"{pb} vs wanted {lb}", 20)
    else:
        add("Bedrooms", "miss", f"{pb} vs wanted {lb}", 20)
    lt, pt = l.get("type"), p.get("type")
    if not lt or not pt:
        add("Type", "unknown", "Not stated", 20)
    elif lt == pt:
        add("Type", "match", lt.title(), 20)
    else:
        add("Type", "miss", f"{pt} vs wanted {lt}", 20)
    return {"score": min(98, round(pts)), "confidence": min(98, known), "criteria": C}


@app.post("/api/match")
def api_match():
    b = request.get_json(silent=True) or {}
    leads, props = b.get("leads") or [], b.get("properties") or []
    if not leads or not props:
        return jsonify(reply="I need results to match. Run Lead Finder and Property Finder first, then come back to Combine.",
                       results=[], notes=[])
    out = []
    for l in leads:
        best = max(({"lead": l, "property": p, **match_pair(l, p)} for p in props), key=lambda m: (m["score"], m["confidence"]))
        out.append(best)
    out.sort(key=lambda m: -m["score"])
    out = out[:8]
    return jsonify(reply=f"Matched {len(out)} buyers with their closest properties.", results=out,
                   notes=["Scores come from location, budget, bedrooms and type. Confidence drops when details are missing."])


# ---------- chat ----------
@app.post("/api/chat")
def api_chat():
    q = ((request.get_json(silent=True) or {}).get("message") or "").strip()
    if not q:
        return jsonify(error="Type a request first."), 400
    intent, p = understand(q)
    if intent == "match":
        return jsonify(intent="match", reply="Matching your newest buyers with properties.", results=[], notes=[])
    items, notes = run_search(intent, q, p)
    if intent == "leads":
        items = sorted(map(score_lead, items), key=lambda l: -l["score"])
        reply = f"Found {len(items)} possible buyers" + (f" for {p['loc']}." if p["loc"] else ".")
    else:
        reply = f"Found {len(items)} listings" + (f" in {p['loc']}." if p["loc"] else ".")
    return jsonify(intent=intent, reply=reply, results=items, notes=notes)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", 5000)))
