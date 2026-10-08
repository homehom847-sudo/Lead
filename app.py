"""SUSU Real Estate AI - Flask backend.  Run: pip install flask && python app.py"""
import json, os, random, re, urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
now = lambda: datetime.now(timezone.utc)


def http(url, body, headers):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def get_keys():
    """Keys come from the browser settings (headers) or env vars. Nothing is hard-coded."""
    h = request.headers
    return {"tavily": h.get("X-Tavily-Key") or os.getenv("TAVILY_API_KEY", ""),
            "gemini": h.get("X-Gemini-Key") or os.getenv("GEMINI_API_KEY", ""),
            "demo": h.get("X-Demo", "1") == "1"}


live_ok = lambda k: bool(k["tavily"]) and not k["demo"]
notice = lambda k: "" if k["demo"] or k["tavily"] else "No Tavily key set, showing demo data."

# ---------- understanding the request ----------
NUM = r"[₦n]?\s*(\d[\d.,]*)\s*(k|m|million|thousand)?"
UNIT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}


def parse_query(q):
    t = q.lower()
    m = re.search(r"(\d+)\s*-?\s*(?:bed|br)", t)
    b = re.search(r"(?:under|below|max|within|budget)(?:\s+(?:of|around|about))?\s*" + NUM, t)
    budget = int(float(b.group(1).rstrip(".,").replace(",", "")) * UNIT.get(b.group(2), 1)) if b else None
    l = re.search(r"\bin\s+([a-z ]+?)(?:\s+(?:under|below|max|within|budget|for)\b|$|[,.?])", t)
    kind = next((k for k in ("apartment", "flat", "house", "duplex", "land", "office", "shop") if k in t), None)
    return {"beds": int(m.group(1)) if m else None, "budget": budget,
            "loc": l.group(1).strip().title() if l else None, "kind": kind}


def intent(q, k):
    p = parse_query(q)
    if k["gemini"] and not k["demo"]:  # optional: let Gemini refine the request
        try:
            prompt = f'Return only JSON {{"beds":int|null,"budget":int NGN|null,"loc":str|null,"kind":str|null}} for: {q}'
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')}:generateContent"
            d = http(url, {"contents": [{"parts": [{"text": prompt}]}],
                           "generationConfig": {"responseMimeType": "application/json"}}, {"x-goog-api-key": k["gemini"]})
            p.update({a: v for a, v in json.loads(d["candidates"][0]["content"]["parts"][0]["text"]).items() if v and a in p})
        except Exception:
            pass  # fall back to the built-in parser
    return {**p, "loc": p["loc"] or "Abuja", "kind": p["kind"] or "apartment"}


# ---------- scoring ----------
def age_hours(s):
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            d = parsedate_to_datetime(s)
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return max(0, (now() - d).total_seconds() / 3600)


def lead_score(l):
    """Recency (decays over 7 days) + clarity (beds, budget, place, intent words). Capped at 97, never 100."""
    h, ex = age_hours(l.get("posted_at")), parse_query(l["snippet"])
    rec = 0 if h is None else max(0, 1 - h / 168)
    clar = sum([bool(ex["beds"]), bool(ex["budget"]), bool(ex["loc"]),
                bool(re.search(r"looking|need|want|searching|buy|rent|anyone", l["snippet"].lower()))]) / 4
    l.update(ex)
    l["score"] = min(97, max(5, round(50 * rec + 43 * clar + (7 if l.get("contact") else 0))))
    l["why"] = {"recency": round(rec * 100), "clarity": round(clar * 100)}
    return l


def match(leads, props):
    out = []
    for l in leads:
        best = None
        for pr in props:
            pts, why = 0, []
            if l.get("loc") and l["loc"].lower() in pr["loc"].lower():
                pts += 35; why.append(f"Location +35: both in {l['loc']}")
            if l.get("budget") and pr.get("price"):
                if pr["price"] <= l["budget"]:
                    pts += 35; why.append(f"Budget +35: ₦{pr['price']:,} fits within ₦{l['budget']:,}")
                elif pr["price"] <= l["budget"] * 1.1:
                    pts += 15; why.append("Budget +15: up to 10% over budget")
            if l.get("beds") and pr.get("beds"):
                d = abs(l["beds"] - pr["beds"])
                if d == 0:
                    pts += 20; why.append(f"Bedrooms +20: both {pr['beds']}")
                elif d == 1:
                    pts += 8; why.append(f"Bedrooms +8: {pr['beds']} vs wanted {l['beds']}")
            qs = round(l["score"] / 10)
            pts += qs; why.append(f"Lead quality +{qs}: lead scored {l['score']}%")
            if not best or pts > best[0]:
                best = (pts, why, pr)
        if best:
            out.append({"id": l["id"], "score": min(98, best[0]), "why": best[1], "lead": l, "property": best[2]})
    return sorted(out, key=lambda m: -m["score"])


# ---------- demo data (deterministic per request, clearly synthetic) ----------
NAMES = ["Amina B.", "Chuka O.", "Tunde A.", "Ngozi E.", "Ibrahim S.", "Funmi K.", "Emeka N.", "Zainab M."]


def svg(h):
    s = (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 320 180'><rect width='320' height='180' fill='hsl({h},45%,72%)'/>"
         f"<path d='M80 110l80-50 80 50v40H80z' fill='hsl({h},40%,38%)'/><rect x='148' y='115' width='24' height='35' fill='hsl({h},45%,88%)'/></svg>")
    return "data:image/svg+xml," + quote(s)


def demo_leads(p):
    r, out = random.Random(json.dumps(p, sort_keys=True)), []
    for i in range(6):
        beds, bud = p["beds"] or r.choice([2, 3, 4]), int((p["budget"] or 5e6) * r.uniform(.8, 1.15) // 5e4 * 5e4)
        if r.random() < .3:
            txt = f"Anyone know a good place in {p['loc']}? Hit me up."
        else:
            txt = (f"{r.choice(['Looking for', 'Need', 'Searching for'])} a {beds} bedroom {p['kind']} in {p['loc']}, "
                   f"budget around ₦{bud:,}. {r.choice(['Please share leads.', 'Need to move next month.', 'Genuine sellers only.'])}")
        name = r.choice(NAMES) if r.random() > .25 else None
        out.append({"id": f"l{i}", "name": name, "snippet": txt, "source": "demo", "url": None,
                    "posted_at": (now() - timedelta(hours=r.choice([1, 3, 6, 14, 30, 60, 120, 200]))).isoformat(),
                    "contact": f"@{name.split()[0].lower()}_demo (public profile)" if name and r.random() < .5 else None})
    return out


def demo_props(p):
    r, out = random.Random("p" + json.dumps(p, sort_keys=True)), []
    for i in range(6):
        beds = p["beds"] or r.choice([2, 3, 4])
        out.append({"id": f"p{i}", "title": f"{beds}-bedroom {p['kind']}", "beds": beds, "kind": p["kind"],
                    "price": int((p["budget"] or 5e6) * r.uniform(.6, 1.1) // 5e4 * 5e4),
                    "loc": f"{r.choice(['GRA', 'Phase 2', 'Central', 'Estate Road', 'Extension'])}, {p['loc']}",
                    "image": svg(r.randint(0, 360)), "source": "demo", "url": None,
                    "posted_at": (now() - timedelta(hours=r.choice([2, 8, 20, 48, 96]))).isoformat()})
    return out


# ---------- live search (Tavily) - public web results only ----------
def tavily(q, key, **kw):
    return http("https://api.tavily.com/search", {"query": q, "max_results": 8, **kw}, {"Authorization": "Bearer " + key})


def num(s, unit):
    try:
        return int(float(s.rstrip(".,").replace(",", "")) * (1e6 if unit else 1))
    except ValueError:
        return None


# ---------- tools (each is a plain function; MCP-ready, see /api/tools) ----------
def find_leads(q, k):
    p = intent(q, k)
    if live_ok(k):
        items = [{"id": f"l{i}", "name": None, "snippet": x["content"][:240], "source": "tavily", "url": x["url"],
                  "posted_at": x.get("published_date"),
                  "contact": next(iter(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", x["content"])), None)}  # only if public in the text
                 for i, x in enumerate(tavily(f"looking to buy or rent {q}, budget, wanted", k["tavily"])["results"])]
    else:
        items = demo_leads(p)
    return {"mode": "live" if live_ok(k) else "demo", "notice": notice(k), "intent": p,
            "items": sorted(map(lead_score, items), key=lambda l: -l["score"])}


def find_properties(q, k):
    p = intent(q, k)
    if live_ok(k):
        d = tavily(f"{q} for sale or rent listing", k["tavily"], include_images=True)
        imgs = [i if isinstance(i, str) else i.get("url") for i in d.get("images", [])]
        items = []
        for i, x in enumerate(d["results"]):
            pr = re.search(r"₦\s*(\d[\d,.]*)\s*(m|million)?", x["content"], re.I)
            items.append({"id": f"p{i}", "title": x["title"][:90], "price": num(pr.group(1), pr.group(2)) if pr else None,
                          "beds": parse_query(x["content"])["beds"], "loc": p["loc"], "source": "tavily", "url": x["url"],
                          "image": imgs[i] if i < len(imgs) else svg(200), "posted_at": x.get("published_date")})
    else:
        items = demo_props(p)
    return {"mode": "live" if live_ok(k) else "demo", "notice": notice(k), "intent": p, "items": items}


def combine(q, k):
    L, P = find_leads(q, k), find_properties(q, k)
    return {**L, "items": match(L["items"], P["items"])[:8]}


TOOLS = {"find_leads": (find_leads, "Search public buyer-intent posts and score them."),
         "find_properties": (find_properties, "Find recently posted property listings."),
         "combine": (combine, "Match leads to properties with an explained score.")}


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/tools")
def tools():  # same shape as an MCP tools/list result, so an MCP adapter can wrap these later
    return jsonify([{"name": n, "description": d, "inputSchema": {"type": "object", "required": ["query"],
                     "properties": {"query": {"type": "string"}}}} for n, (_, d) in TOOLS.items()])


@app.post("/api/tools/<name>")
def call(name):
    if name not in TOOLS:
        return jsonify(error="Unknown tool."), 404
    q = ((request.get_json(silent=True) or {}).get("query") or "").strip()
    if not q:
        return jsonify(error="Type a request first."), 400
    try:
        return jsonify(TOOLS[name][0](q, get_keys()))
    except Exception as e:
        return jsonify(error=f"Live search failed ({e}). Turn on Demo Mode in Settings to keep going."), 502


if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", 5000)))
