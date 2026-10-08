# SUSU Real Estate AI

One page, three modes (Lead Finder, Property Finder, Combine). Demo Mode works with no keys.

## Run (Termux or any machine)
    pkg install python        # Termux only
    pip install -r requirements.txt
    python app.py             # open http://127.0.0.1:5000

## Go live later
Open Settings (gear icon), turn off Demo Mode, paste your Tavily key (Gemini optional).
Or set env vars: TAVILY_API_KEY, GEMINI_API_KEY (optional GEMINI_MODEL).

## MCP-ready
Each feature is a plain function in app.py's TOOLS registry, exposed at
GET /api/tools and POST /api/tools/<name> in MCP tools/list shape.
Later, wrap TOOLS in an MCP server or call external MCP tools inside those functions.

## Rules baked in
Public web results only. Contact shown only if it appears in the public text.
Lead scores are capped at 97, match scores at 98.
