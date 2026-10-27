"""本地标注台的页面（单文件、无构建、无外部依赖）。"""

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>小弗 · 回复标注台</title>
<style>
  :root { color-scheme: dark; --bg:#111317; --panel:#1a1d23; --line:#2a2f38;
          --text:#e6e8ec; --dim:#8b93a1; --accent:#4f8cff; --good:#3ecf8e; --bad:#ff6b6b; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
  header { position:sticky; top:0; z-index:5; background:rgba(17,19,23,.92);
           backdrop-filter:blur(8px); border-bottom:1px solid var(--line); padding:14px 22px;
           display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
  h1 { font-size:16px; margin:0 8px 0 0; font-weight:600; }
  header input[type=date] { background:var(--panel); color:var(--text);
           border:1px solid var(--line); border-radius:8px; padding:6px 10px; }
  .stat { color:var(--dim); font-size:13px; }
  .stat b { color:var(--text); font-weight:600; }
  main { max-width:860px; margin:26px auto 120px; padding:0 18px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:14px;
          padding:22px; box-shadow:0 10px 30px rgba(0,0,0,.25); }
  .meta { color:var(--dim); font-size:12px; margin-bottom:14px; display:flex; gap:14px; }
  .bubble { border-radius:12px; padding:12px 14px; margin:10px 0; white-space:pre-wrap;
            overflow-wrap:anywhere; }
  .user { background:#20242b; border-left:3px solid #5a6472; }
  .bot { background:#17251f; border-left:3px solid var(--good); }
  .tag { font-size:11px; color:var(--dim); display:block; margin-bottom:4px; }
  .scores { display:grid; grid-template-columns:repeat(11, 1fr); gap:6px; margin:18px 0 10px; }
  .scores button { padding:10px 0; border-radius:8px; border:1px solid var(--line);
                   background:#20242b; color:var(--text); cursor:pointer; font-size:14px; }
  .scores button:hover { border-color:var(--accent); }
  .scores button.on { background:var(--accent); border-color:var(--accent); color:#fff; }
  .low { color:var(--bad); } .high { color:var(--good); }
  textarea { width:100%; min-height:54px; resize:vertical; background:#14171c;
             color:var(--text); border:1px solid var(--line); border-radius:8px; padding:10px; }
  .nav { display:flex; gap:10px; margin-top:16px; flex-wrap:wrap; }
  .nav button, .finish { padding:9px 16px; border-radius:8px; border:1px solid var(--line);
             background:#20242b; color:var(--text); cursor:pointer; }
  .nav button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  .finish { margin-left:auto; }
  .finish:hover { border-color:var(--good); }
  .progress { height:6px; background:#20242b; border-radius:99px; overflow:hidden; margin:14px 0 0; }
  .progress span { display:block; height:100%; background:var(--accent); width:0; transition:width .2s; }
  .empty { text-align:center; color:var(--dim); padding:60px 0; }
  .toast { position:fixed; left:50%; bottom:26px; transform:translateX(-50%);
           background:#20242b; border:1px solid var(--line); border-radius:10px;
           padding:10px 16px; color:var(--text); opacity:0; transition:opacity .2s; pointer-events:none; }
  .toast.show { opacity:1; }
  kbd { background:#20242b; border:1px solid var(--line); border-radius:5px; padding:1px 5px; font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>🐟 回复标注台</h1>
  <input type="date" id="date">
  <div class="stat">进度 <b id="progressText">0 / 0</b></div>
  <div class="stat">平均分 <b id="avg">-</b></div>
  <div class="stat">好评率(>=7) <b id="goodRate">-</b></div>
</header>

<main>
  <div class="card" id="card">
    <div class="empty" id="empty">加载中…</div>
    <div id="body" hidden>
      <div class="meta"><span id="chat"></span><span id="time"></span><span id="idx"></span></div>
      <div class="bubble user"><span class="tag">用户</span><span id="user"></span></div>
      <div class="bubble bot"><span class="tag">小弗</span><span id="assistant"></span></div>
      <div class="scores" id="scores"></div>
      <textarea id="note" placeholder="备注（可选）：为什么好/为什么差"></textarea>
      <div class="progress"><span id="bar"></span></div>
      <div class="nav">
        <button id="prev">← 上一条</button>
        <button id="next">下一条 →</button>
        <button class="primary" id="saveNext">保存并下一条</button>
        <button class="finish" id="finish">提交今日并导出</button>
      </div>
      <div class="stat" style="margin-top:12px">
        快捷键：<kbd>0</kbd>-<kbd>9</kbd> 打分，<kbd>a</kbd>=10，<kbd>←</kbd>/<kbd>→</kbd> 切换
      </div>
    </div>
  </div>
</main>
<div class="toast" id="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
let items = [], index = 0, current = null;
const today = new Date().toLocaleDateString('sv-SE');

$('date').value = today;

function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('show'), 1600);
}

async function load(date) {
  const resp = await fetch(`/api/items?date=${date}`);
  const data = await resp.json();
  items = data.items || [];
  index = items.findIndex((i) => i.score === null);
  if (index < 0) index = 0;
  renderStats(data.stats);
  render();
}

function renderStats(stats) {
  if (!stats) return;
  $('progressText').textContent = `${stats.scored} / ${stats.total}`;
  $('avg').textContent = stats.average ?? '-';
  let good = 0, scored = 0;
  for (let s = 0; s <= 10; s++) { scored += stats.histogram[String(s)] || 0; if (s >= 7) good += stats.histogram[String(s)] || 0; }
  $('goodRate').textContent = scored ? Math.round(good / scored * 100) + '%' : '-';
  $('bar').style.width = stats.total ? (stats.scored / stats.total * 100) + '%' : '0';
}

function render() {
  current = items[index] || null;
  if (!current) {
    $('empty').hidden = false;
    $('body').hidden = true;
    $('empty').textContent = items.length ? '都打完了，点右上角提交导出。' : '这一天没有需要标注的回复。';
    return;
  }
  $('empty').hidden = true;
  $('body').hidden = false;
  $('chat').textContent = current.chat_id;
  $('time').textContent = current.created_at.replace('T', ' ').slice(5, 16);
  $('idx').textContent = `${index + 1} / ${items.length}`;
  $('user').textContent = current.user_message || '（没有上文）';
  $('assistant').textContent = current.assistant;
  $('note').value = current.note || '';

  const box = $('scores');
  box.innerHTML = '';
  for (let s = 0; s <= 10; s++) {
    const b = document.createElement('button');
    b.textContent = s;
    b.className = (current.score === s ? 'on ' : '') + (s <= 3 ? 'low' : s >= 7 ? 'high' : '');
    b.onclick = () => save(s);
    box.appendChild(b);
  }
}

async function save(score) {
  if (!current) return;
  const note = $('note').value.trim();
  const resp = await fetch('/api/score', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message_id: current.message_id, score, note }),
  });
  if (!resp.ok) { toast('保存失败：' + (await resp.text())); return; }
  current.score = score;
  current.note = note;
  const stats = await (await fetch(`/api/stats?date=${$('date').value}`)).json();
  renderStats(stats);
  toast(`已记 ${score} 分`);
  if (index < items.length - 1) { index++; render(); } else { render(); }
}

$('prev').onclick = () => { if (index > 0) { index--; render(); } };
$('next').onclick = () => { if (index < items.length - 1) { index++; render(); } };
$('saveNext').onclick = () => {
  if (!current) return;
  if (current.score === null) { toast('先点一个分数'); return; }
  save(current.score);
};

$('finish').onclick = async () => {
  const resp = await fetch('/api/finish', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ date: $('date').value }),
  });
  const data = await resp.json();
  if (!resp.ok) { toast('导出失败：' + (data.detail || '')); return; }
  toast(`已导出 ${data.exported} 条 → ${data.path}`);
};

$('date').onchange = (e) => load(e.target.value);

document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
  if (e.key >= '0' && e.key <= '9') save(Number(e.key));
  else if (e.key.toLowerCase() === 'a') save(10);
  else if (e.key === 'ArrowLeft') $('prev').click();
  else if (e.key === 'ArrowRight') $('next').click();
});

load(today);
</script>
</body>
</html>
"""
