"""场次列表页（本地服务的首页）：挑一个场次打开，外加导入块。

从 ``server.py`` 里分出来的（ticket #32）：加一个"导入前预览"面板就让它多出 296 行，
而 ``TestStructureOfTheSplit`` 那条守卫写得很清楚——``server.py`` 只该收字节、发字节。
这里全是字符串拼页面，没有一处碰 socket。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # 只为类型标注，运行时不 import（避免绕圈）
    from .library import SessionLibrary


_IMPORT_BLOCK = """
<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;
            background:#171a21;border:1px solid #2b313c;border-radius:8px;
            padding:10px 12px;margin:0 0 14px">
  <input id="files" type="file" multiple accept=".ld,.ldx,.csv,.xlsx,.txt,.tsv" style="display:none">
  <button id="pickBtn" style="background:#1d3b52;border:1px solid #4cc2ff;color:#e6e9ef;
          border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px">
    选择日志文件导入
  </button>
  <span style="color:#8b94a7;font-size:12px">
    .ld / .ldx / .csv / .xlsx / .txt / .tsv · 或者把文件直接拖进这个窗口 · 也可以拖到 <code>导入数据.bat</code> 上
  </span>
  <span id="importMsg" style="color:#4cc2ff;font-size:12px;margin-left:auto"></span>
</div>
<div id="importCard" style="display:none;background:#171a21;border:1px solid #2b313c;
            border-radius:8px;padding:12px;margin:0 0 14px">
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 8px">
    <b id="cardHead" style="font-size:13px"></b>
    <span id="cardInfo" style="color:#8b94a7;font-size:12px"></span>
  </div>
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 8px;
              font-size:12px;color:#c8cfdc">
    <label>分隔符 <select id="optDelimiter"></select></label>
    <label>编码 <select id="optEncoding"></select></label>
    <label>表头行 <select id="optHeader"></select></label>
    <label>单位行 <select id="optUnitRow">
      <option value="">自动认</option><option value="1">有</option><option value="0">没有</option>
    </select></label>
    <label>没有时间列时按 <input id="optRate" type="number" min="0" step="1"
      placeholder="Hz" style="width:64px;background:#0f1115;color:#e6e9ef;
      border:1px solid #2b313c;border-radius:4px;padding:2px 4px"> Hz 生成，
      起点 <input id="optStart" type="number" step="any" placeholder="0"
      style="width:64px;background:#0f1115;color:#e6e9ef;border:1px solid #2b313c;
      border-radius:4px;padding:2px 4px"> s</label>
    <button id="cardOk" style="background:#1d3b52;border:1px solid #4cc2ff;color:#e6e9ef;
            border-radius:6px;padding:5px 12px;cursor:pointer;font-size:12px">导入</button>
    <button id="cardCancel" style="background:#1a1d24;border:1px solid #2b313c;color:#8b94a7;
            border-radius:6px;padding:5px 12px;cursor:pointer;font-size:12px">取消</button>
  </div>
  <div id="cardError" style="display:none;color:#ff5d6c;font-size:12px;margin:0 0 8px"></div>
  <div id="cardTable" style="max-height:260px;overflow:auto;font-size:12px"></div>
</div>
<script>
(function () {
  var input = document.getElementById("files");
  var msg = document.getElementById("importMsg");
  var card = document.getElementById("importCard");
  var head = document.getElementById("cardHead");
  var info = document.getElementById("cardInfo");
  var errorBox = document.getElementById("cardError");
  var tableBox = document.getElementById("cardTable");
  var busy = false;
  var settle = null;
  var token = "";

  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  // 只有分隔文本需要"导入前看一眼"。.ld / .xlsx 的读法是确定的，问了也是白问。
  function needsPreview(name) { return /\\.(csv|txt|tsv)$/i.test(name); }

  function options() {
    var parts = [];
    var pairs = [["delimiter", "optDelimiter"], ["encoding", "optEncoding"],
                 ["header", "optHeader"], ["unit_row", "optUnitRow"],
                 ["generate_rate", "optRate"], ["generate_start", "optStart"]];
    for (var i = 0; i < pairs.length; i++) {
      var value = String(document.getElementById(pairs[i][1]).value || "").trim();
      if (value && value !== "auto") {
        parts.push(pairs[i][0] + "=" + encodeURIComponent(value));
      }
    }
    return parts.join("&");
  }

  function fill(select, rows, first) {
    if (select.options.length) return;
    var html = first ? '<option value="">' + esc(first) + "</option>" : "";
    rows.forEach(function (row) {
      html += '<option value="' + esc(row.value) + '">' + esc(row.label) + "</option>";
    });
    select.innerHTML = html;
  }

  // 候选项由服务端给（分隔符/编码各一份定义），界面不另抄一份。
  function buildSelects(p) {
    fill(document.getElementById("optDelimiter"), p.delimiters || [], "自动认");
    fill(document.getElementById("optEncoding"), p.encodings || [], "自动认");
    var header = document.getElementById("optHeader");
    if (!header.options.length) {
      var html = '<option value="auto">自动认</option>';
      for (var i = 1; i <= 12; i++) html += '<option value="' + i + '">第 ' + i + " 行</option>";
      html += '<option value="none">没有表头行</option>';
      header.innerHTML = html;
    }
  }

  function render(p, name, size) {
    buildSelects(p);
    head.textContent = name + " · " + (size / 1e6).toFixed(2) + " MB";
    // 下拉框显示**实际会用**的那一项（自动认出来的也写进去），否则改了看不出效果。
    if (p.delimiter !== undefined) document.getElementById("optDelimiter").value = p.delimiter;
    if (p.encoding) document.getElementById("optEncoding").value = p.encoding;
    document.getElementById("optHeader").value =
      p.effective_header < 0 ? "none" : String(p.effective_header + 1);
    document.getElementById("optUnitRow").value = p.effective_unit_row ? "1" : "0";
    var rows = p.rows || [];
    var cells = rows.slice(0, 12).map(function (row) {
      return "<tr>" + row.slice(0, 10).map(function (cell) {
        return '<td style="padding:2px 8px;border-bottom:1px solid #232833;white-space:nowrap">'
          + esc(cell) + "</td>";
      }).join("") + "</tr>";
    }).join("");
    tableBox.innerHTML = cells ? '<table style="border-collapse:collapse">' + cells + "</table>" : "";
    if (p.error) {
      errorBox.style.display = "block";
      errorBox.textContent = p.error;
      info.textContent = "";
      return;
    }
    errorBox.style.display = "none";
    var where = p.effective_header < 0 ? "没有表头行" : "表头第 " + (p.effective_header + 1) + " 行";
    info.textContent = "✓ " + (p.channels || []).length + " 通道 · " + where
      + (p.effective_unit_row ? " + 单位行" : "") + " · 共 " + p.width + " 列"
      + " · " + Number(p.sample_rate).toFixed(3).replace(/\\.?0+$/, "") + " Hz"
      + (p.partial ? "（按前 " + p.rows_shown + " 行预览）" : "")
      + (p.parse_note ? " · " + p.parse_note : "");
  }

  function finish(ok) { if (settle) { var done = settle; settle = null; done(ok); } }

  async function refresh(name, size) {
    var query = options();
    var res = await fetch("/api/import/preview?token=" + encodeURIComponent(token)
                          + (query ? "&" + query : ""));
    var body = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(body.error || ("HTTP " + res.status));
    render(body.preview || {}, name, size);
  }

  ["optDelimiter", "optEncoding", "optHeader", "optUnitRow", "optRate", "optStart"]
    .forEach(function (id) {
      document.getElementById(id).addEventListener("change", function () {
        refresh(head.dataset.name, Number(head.dataset.size)).catch(showError);
      });
    });

  function showError(err) {
    errorBox.style.display = "block";
    errorBox.textContent = err.message;
  }

  document.getElementById("cardCancel").addEventListener("click", function () { finish(false); });
  document.getElementById("cardOk").addEventListener("click", async function () {
    var query = options();
    try {
      var res = await fetch("/api/import/commit?token=" + encodeURIComponent(token)
                            + (query ? "&" + query : ""), { method: "POST" });
      var body = await res.json().catch(function () { return {}; });
      if (!res.ok) throw new Error(body.error || ("HTTP " + res.status));
      msg.style.color = "#4cc2ff";
      msg.textContent = "已导入 " + body.file
        + (body.channels ? " · " + body.channels + " 通道" : "")
        + (body.warning ? " · " + body.warning : "");
      finish(true);
    } catch (err) { showError(err); }
  });

  async function stage(file) {
    var res = await fetch("/api/import?name=" + encodeURIComponent(file.name),
                          { method: "PUT", body: file });
    var body = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(body.error || ("HTTP " + res.status));
    return body;
  }

  async function oneStep(file) {
    var res = await fetch("/api/upload?name=" + encodeURIComponent(file.name),
                          { method: "PUT", body: file });
    var body = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(body.error || ("HTTP " + res.status));
  }

  document.getElementById("pickBtn").addEventListener("click", function () {
    if (!busy) input.click();
  });

  async function upload(list) {
    var files = Array.prototype.slice.call(list);
    if (!files.length || busy) return;
    busy = true;
    msg.style.color = "#4cc2ff";
    try {
      for (var i = 0; i < files.length; i++) {
        var file = files[i];
        msg.textContent = (i + 1) + "/" + files.length + ": " + file.name
          + " (" + (file.size / 1e6).toFixed(1) + " MB) …";
        if (!needsPreview(file.name)) { await oneStep(file); continue; }
        var staged = await stage(file);
        token = staged.token;
        head.dataset.name = staged.file;
        head.dataset.size = String(staged.bytes);
        ["optDelimiter", "optEncoding", "optHeader"].forEach(function (id) {
          document.getElementById(id).innerHTML = "";
        });
        card.style.display = "block";
        await refresh(staged.file, staged.bytes);
        var ok = await new Promise(function (resolve) { settle = resolve; });
        card.style.display = "none";
        if (!ok) {
          await fetch("/api/import/cancel?token=" + encodeURIComponent(staged.token),
                      { method: "DELETE" });
          msg.textContent = "已取消 " + file.name;
        }
      }
      msg.textContent = "导入完成，正在刷新…";
      location.reload();
    } catch (err) {
      msg.style.color = "#ff5d6c";
      msg.textContent = "导入失败 — " + err.message;
      card.style.display = "none";
      busy = false;
    }
  }

  input.addEventListener("change", function () { upload(input.files); });
  document.addEventListener("dragover", function (e) { e.preventDefault(); });
  document.addEventListener("drop", function (e) {
    e.preventDefault();
    if (e.dataTransfer && e.dataTransfer.files) upload(e.dataTransfer.files);
  });
})();
</script>
"""


def index_page(library: SessionLibrary, error: str | None = None) -> str:
    """A no-frills session picker; the real UI is the workbench itself."""
    if error:
        return (
            "<!doctype html><meta charset='utf-8'><title>i3pro</title>"
            "<body style='font:14px system-ui;padding:32px;background:#0f1115;color:#e6e9ef'>"
            f"<h1>i3pro 本地服务</h1><p>{error}</p>"
            "</body>"
        )
    rows = []
    for entry in library.listing():
        if "error" in entry:
            rows.append(
                f"<tr><td>{entry['name']}</td><td colspan='5' style='color:#ff5d6c'>"
                f"{entry['error']}</td></tr>"
            )
            continue
        best = "--" if entry.get("best_lap") is None else f"{entry['best_lap']:.3f} s"
        rows.append(
            "<tr>"
            f"<td><a href='{entry['url']}'>{entry['name']}</a></td>"
            f"<td>{entry.get('device', '')}</td>"
            f"<td>{entry.get('log_date', '')} {entry.get('log_time', '')}</td>"
            f"<td>{entry.get('duration', 0):.0f} s</td>"
            f"<td>{entry.get('channels', 0)}</td>"
            f"<td>{entry.get('complete_laps', 0)}</td>"
            f"<td>{best}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<meta charset="utf-8">
<title>i3pro - 场次</title>
<style>
 body {{ margin:0; background:#0f1115; color:#e6e9ef;
        font:14px/1.5 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }}
 header {{ padding:22px 28px; border-bottom:1px solid #2b313c; }}
 h1 {{ margin:0 0 4px; font-size:18px; }}
 p {{ margin:0; color:#8b94a7; }}
 main {{ padding:18px 28px; }}
 table {{ border-collapse:collapse; width:100%; }}
 th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid #2b313c; }}
 th {{ color:#8b94a7; font-weight:600; }}
 a {{ color:#4cc2ff; text-decoration:none; }}
 a:hover {{ text-decoration:underline; }}
</style>
<header>
  <h1>i3pro 本地服务</h1>
  <p>选择一个试车场次开始分析；所有数据都在本机解析，不上传。</p>
</header>
<main>
{_IMPORT_BLOCK}
<table>
 <tr><th>场次</th><th>设备</th><th>日期</th><th>时长</th><th>通道</th><th>完整圈</th><th>最快圈</th></tr>
 {''.join(rows) or '<tr><td colspan="7">没有找到场次文件（.ld / .csv / .xlsx / .txt / .tsv）</td></tr>'}
</table>
</main>
"""
