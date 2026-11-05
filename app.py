import os, sqlite3, secrets, contextlib, json
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from mcp.server.fastmcp import FastMCP

DB = os.environ.get("DB_PATH", "data.db")
BASE = os.environ.get("BASE_URL", "http://localhost:8000")


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


with db() as c:
    c.execute("CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY, title TEXT, status TEXT DEFAULT 'draft')")
    c.execute("""CREATE TABLE IF NOT EXISTS expenses(
        id INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT,
        merchant TEXT, date TEXT, category TEXT, amount REAL, tax REAL)""")


def insert_rows(c, rid, items):
    for e in items:
        c.execute(
            "INSERT INTO expenses(report_id,merchant,date,category,amount,tax) VALUES(?,?,?,?,?,?)",
            (rid, str(e.get("merchant", "")), str(e.get("date", "")), str(e.get("category", "Other")),
             float(e.get("amount") or 0), float(e.get("tax") or 0)),
        )


def load_report(rid):
    with db() as c:
        r = c.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
        if not r:
            return None
        rows = c.execute(
            "SELECT merchant,date,category,amount,tax FROM expenses WHERE report_id=? ORDER BY id", (rid,)
        ).fetchall()
    return {"id": r["id"], "title": r["title"], "status": r["status"], "expenses": [dict(x) for x in rows]}


# ---------- MCP (what Claude talks to) ----------
mcp = FastMCP("Expense Reports", host="0.0.0.0", stateless_http=True, json_response=True)


@mcp.tool()
def create_report(title: str, expenses: list[dict]) -> dict:
    """Create a new expense report from receipts you have read.
    Each expense needs: merchant (str), date (YYYY-MM-DD), category (e.g. Travel, Meals,
    Software, Office, Other), amount (number, total paid), tax (number, 0 if unknown).
    Returns an edit_url. Always give this link to the user so they can review and edit."""
    rid = secrets.token_urlsafe(8)
    with db() as c:
        c.execute("INSERT INTO reports(id,title) VALUES(?,?)", (rid, title))
        insert_rows(c, rid, expenses)
    return {"report_id": rid, "edit_url": f"{BASE}/r/{rid}", "count": len(expenses)}


@mcp.tool()
def add_expenses(report_id: str, expenses: list[dict]) -> dict:
    """Add more expenses (same fields as create_report) to an existing report."""
    if not load_report(report_id):
        return {"error": "report not found"}
    with db() as c:
        insert_rows(c, report_id, expenses)
    return {"edit_url": f"{BASE}/r/{report_id}", "added": len(expenses)}


@mcp.tool()
def get_report(report_id: str) -> dict:
    """Read a report, including any edits the human made."""
    return load_report(report_id) or {"error": "report not found"}


mcp_app = mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


# ---------- Website (what the human uses) ----------
app = FastAPI(lifespan=lifespan)

PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Expense Report</title>
<style>
body{font-family:system-ui;max-width:900px;margin:30px auto;padding:0 16px;color:#111}
input,select{padding:6px;border:1px solid #ccc;border-radius:6px;width:100%;box-sizing:border-box}
table{width:100%;border-collapse:collapse}td,th{padding:4px;text-align:left}
button{padding:10px 16px;border:0;border-radius:8px;background:#4f46e5;color:#fff;cursor:pointer;margin:8px 8px 8px 0}
#title{font-size:24px;font-weight:700}.total{font-size:20px;margin:16px 0}
</style></head><body>
<input id=title>
<p>Status: <select id=status style="width:auto"><option>draft</option><option>submitted</option><option>approved</option></select></p>
<table><thead><tr><th>Merchant<th>Date<th>Category<th>Amount<th>Tax<th></tr></thead><tbody id=rows></tbody></table>
<button onclick="addRow()">+ Add row</button><button onclick="save()">Save</button>
<a href="/r/__ID__/csv">Download CSV</a>
<div class=total>Total: <b id=total>0</b> (tax: <span id=tax>0</span>)</div><div id=msg></div>
<script>
const ID="__ID__";let data=__DATA__;
const F=["merchant","date","category","amount","tax"];
function render(){
  document.getElementById("title").value=data.title;
  document.getElementById("status").value=data.status;
  const tb=document.getElementById("rows");tb.innerHTML="";
  data.expenses.forEach((e,i)=>{
    const tr=document.createElement("tr");
    F.forEach(f=>{const td=document.createElement("td");const inp=document.createElement("input");
      inp.value=e[f];inp.oninput=()=>{e[f]=inp.value;calc()};td.appendChild(inp);tr.appendChild(td)});
    const td=document.createElement("td");const b=document.createElement("button");
    b.textContent="x";b.style.background="#dc2626";b.onclick=()=>{data.expenses.splice(i,1);render()};
    td.appendChild(b);tr.appendChild(td);tb.appendChild(tr)});
  calc();
}
function calc(){
  let t=0,x=0;data.expenses.forEach(e=>{t+=parseFloat(e.amount)||0;x+=parseFloat(e.tax)||0});
  document.getElementById("total").textContent=t.toFixed(2);
  document.getElementById("tax").textContent=x.toFixed(2);
}
function addRow(){data.expenses.push({merchant:"",date:"",category:"Other",amount:0,tax:0});render()}
async function save(){
  data.title=document.getElementById("title").value;
  data.status=document.getElementById("status").value;
  const r=await fetch("/api/r/"+ID,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
  document.getElementById("msg").textContent=r.ok?"Saved ✓":"Error saving";
}
render();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return "<h2>Receipt → Expense Report</h2><p>Connect this app to Claude via /mcp</p>"


@app.get("/r/{rid}", response_class=HTMLResponse)
def view(rid: str):
    rep = load_report(rid)
    if not rep:
        return HTMLResponse("Not found", status_code=404)
    data = json.dumps(rep).replace("</", "<\\/")
    return PAGE.replace("__ID__", rid).replace("__DATA__", data)


@app.post("/api/r/{rid}")
async def save(rid: str, request: Request):
    body = await request.json()
    if not load_report(rid):
        return JSONResponse({"error": "not found"}, status_code=404)
    with db() as c:
        c.execute("UPDATE reports SET title=?, status=? WHERE id=?", (body["title"], body["status"], rid))
        c.execute("DELETE FROM expenses WHERE report_id=?", (rid,))
        insert_rows(c, rid, body["expenses"])
    return {"ok": True}


@app.get("/r/{rid}/csv")
def csv_export(rid: str):
    from fastapi.responses import PlainTextResponse
    rep = load_report(rid)
    if not rep:
        return PlainTextResponse("Not found", status_code=404)
    lines = ["merchant,date,category,amount,tax"]
    for e in rep["expenses"]:
        lines.append(f'"{e["merchant"]}",{e["date"]},{e["category"]},{e["amount"]},{e["tax"]}')
    return PlainTextResponse("\n".join(lines), media_type="text/csv")


# Mount the MCP server last. It serves /mcp
app.mount("/", mcp_app)