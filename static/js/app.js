(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let meta = null;
  let batches = [];
  let selected = null;
  let detail = null;
  let reports = [];
  let conflicts = [];
  let form = {
    object_key: "",
    content_hash: "",
    byte_size: "100",
    on_conflict: "reject",
    repackage: false,
  };

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || j.detail || ("HTTP " + r.status));
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  async function refresh() {
    meta = await api("/api/meta");
    document.getElementById("lab-brand").textContent = meta.lab_name || "湖区清单台";
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/visibility-reports")).items || [];
    conflicts = (await api("/api/conflicts")).items || [];
  }

  async function openBatch(id) {
    selected = id;
    detail = await api("/api/batches/" + id);
    tab = "detail";
    syncNav();
    render();
  }

  async function doRegister() {
    if (!selected) return;
    setMsg("");
    try {
      const r = await api("/api/batches/" + selected + "/objects", {
        method: "POST",
        body: JSON.stringify({
          object_key: form.object_key,
          content_hash: form.content_hash,
          byte_size: Number(form.byte_size || 0),
          on_conflict: form.on_conflict,
          repackage: form.repackage,
        }),
      });
      form.object_key = "";
      form.content_hash = "";
      form.repackage = false;
      setMsg(r.message || "已登记对象");
    } catch (e) {
      // 失败（含拒登）也要刷新三面：对象行可能已上锁、冲突队列已记结局
      setMsg(e.message);
    }
    await openBatch(selected);
    await refresh();
    render();
  }

  async function doRecheck(objectId) {
    setMsg("");
    try {
      const r = await api("/api/objects/" + objectId + "/recheck", {
        method: "POST",
        body: "{}",
      });
      setMsg(r.message || "复检通过，已放行");
    } catch (e) {
      setMsg(e.message);
    }
    if (selected) await openBatch(selected);
    await refresh();
    render();
  }

  async function doClose() {
    if (!selected) return;
    setMsg("");
    try {
      await api("/api/batches/" + selected + "/close", { method: "POST", body: "{}" });
      await openBatch(selected);
      await refresh();
      setMsg("清单已闭合");
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doReport() {
    setMsg("");
    try {
      const r = await api("/api/visibility-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date: "2026-11-16" }),
      });
      await refresh();
      tab = "reports";
      syncNav();
      setMsg(r.report.summary);
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  function syncNav() {
    document.querySelectorAll("nav [data-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
  }

  function strategyLabel(s) {
    return s === "rebuild" ? "重建指纹" : "拒绝锁定";
  }

  function outcomeChip(outcome) {
    const cls = outcome === "已重建" ? "chip chip-rebuild" : "chip chip-reject";
    return `<span class="${cls}">${outcome}</span>`;
  }

  function objectStatus(o) {
    let html = `<span class="pill">${o.status}</span>`;
    if (o.recheck) html += ` <span class="chip chip-warn">待复检</span>`;
    if (o.locked) html += ` <span class="chip chip-reject">已锁定</span>`;
    return html;
  }

  function conflictTable(rows, withBatch) {
    return `<table>
      <thead><tr>
        ${withBatch ? "<th>批次</th>" : ""}<th>编号</th><th>对象键</th><th>旧指纹</th><th>新指纹</th><th>策略</th><th>结局</th><th>灌数员</th><th>时间</th>
      </tr></thead>
      <tbody>
        ${rows
          .map(
            (c) => `<tr>
          ${withBatch ? `<td>#${c.batch_id}</td>` : ""}<td>${c.id}</td><td>${c.object_key}</td><td>${c.old_hash}</td><td>${c.new_hash}</td>
          <td>${strategyLabel(c.strategy)}</td><td>${outcomeChip(c.outcome)}</td>
          <td>${c.actor || "—"}</td><td>${c.created_at}</td>
        </tr>`
          )
          .join("")}
      </tbody>
    </table>`;
  }

  function renderBatches() {
    return `
      <h2>落地批次</h2>
      <p class="muted">主链：选批 → 登记对象 → 闭合清单 → 可见性对账</p>
      <table>
        <thead><tr><th>编号</th><th>湖区</th><th>业务日</th><th>状态</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${b.id}</td>
            <td>${b.zone_name}<div class="muted">${b.zone_code}</div></td>
            <td>${b.biz_date}</td>
            <td><span class="pill">${b.status}</span></td>
            <td><button type="button" class="secondary" data-open="${b.id}">打开</button></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderDetail() {
    if (!detail) {
      return `<h2>批次详情</h2><p class="muted">请先从落地批次打开一条。</p>`;
    }
    const b = detail.batch;
    const objs = detail.objects || [];
    const confs = detail.conflicts || [];
    return `
      <h2>批次详情 #${b.id}</h2>
      <p>${b.zone_name} · ${b.biz_date} · <span class="pill">${b.status}</span></p>
      <div class="row-actions">
        <button type="button" id="btn-close">闭合清单</button>
        <button type="button" class="secondary" id="btn-report-from-detail">跑可见性对账</button>
      </div>
      <div class="panel">
        <h3>登记对象（去重灌库）</h3>
        <p class="muted">同键同指纹幂等放行；同键不同指纹按互斥策略一次落成，结局进冲突队列。</p>
        <label>对象键<input id="f-key" value="${form.object_key}" /></label>
        <label>内容指纹<input id="f-hash" value="${form.content_hash}" /></label>
        <label>字节数<input id="f-size" value="${form.byte_size}" /></label>
        <div class="strategy-row">
          <span class="muted">指纹失配策略（互斥）：</span>
          <label class="inline"><input type="radio" name="onconf" value="reject" ${form.on_conflict === "reject" ? "checked" : ""} /> 拒绝锁定（须重打包）</label>
          <label class="inline"><input type="radio" name="onconf" value="rebuild" ${form.on_conflict === "rebuild" ? "checked" : ""} /> 重建指纹（强制复检）</label>
        </div>
        <label class="inline"><input type="checkbox" id="f-repack" ${form.repackage ? "checked" : ""} /> 重打包解锁（仅对已锁定键，须带原指纹）</label>
        <div><button type="button" id="btn-register">写入登记</button></div>
      </div>
      <h3>对象列表</h3>
      <table>
        <thead><tr><th>键</th><th>指纹</th><th>字节</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          ${objs
            .map(
              (o) => `<tr>
            <td>${o.object_key}</td><td>${o.content_hash}</td><td>${o.byte_size}</td>
            <td>${objectStatus(o)}</td>
            <td>${o.recheck ? `<button type="button" class="secondary" data-recheck="${o.id}">复检放行</button>` : ""}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      <h3>本批冲突结局</h3>
      ${confs.length ? conflictTable(confs, false) : `<p class="muted">暂无冲突记录。</p>`}`;
  }

  function renderConflicts() {
    return `
      <h2>冲突队列</h2>
      <p class="muted">同键不同指纹的灌库结局芯片：拒登锁定须重打包，或重建指纹强制复检；去重灌库可据此复验。</p>
      ${conflicts.length ? conflictTable(conflicts, true) : `<p class="muted">暂无冲突记录。</p>`}`;
  }

  function renderReports() {
    return `
      <h2>可见性对账</h2>
      <button type="button" id="btn-report">生成对账</button>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>错位</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td><td>${r.as_of_date}</td><td>${r.mismatch_count}</td><td>${r.summary}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function render() {
    if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "detail") view.innerHTML = renderDetail();
    else if (tab === "conflicts") view.innerHTML = renderConflicts();
    else view.innerHTML = renderReports();

    view.querySelectorAll("[data-open]").forEach((btn) => {
      btn.onclick = () => openBatch(Number(btn.dataset.open));
    });
    const reg = document.getElementById("btn-register");
    if (reg) {
      const key = document.getElementById("f-key");
      const hash = document.getElementById("f-hash");
      const size = document.getElementById("f-size");
      const repack = document.getElementById("f-repack");
      key.oninput = () => (form.object_key = key.value);
      hash.oninput = () => (form.content_hash = hash.value);
      size.oninput = () => (form.byte_size = size.value);
      repack.onchange = () => (form.repackage = repack.checked);
      document.querySelectorAll('input[name="onconf"]').forEach((radio) => {
        radio.onchange = () => (form.on_conflict = radio.value);
      });
      reg.onclick = doRegister;
    }
    view.querySelectorAll("[data-recheck]").forEach((btn) => {
      btn.onclick = () => doRecheck(Number(btn.dataset.recheck));
    });
    const closeBtn = document.getElementById("btn-close");
    if (closeBtn) closeBtn.onclick = doClose;
    const r1 = document.getElementById("btn-report");
    const r2 = document.getElementById("btn-report-from-detail");
    if (r1) r1.onclick = doReport;
    if (r2) r2.onclick = doReport;
  }

  document.querySelectorAll("nav [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncNav();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(render)
    .catch((e) => setMsg(e.message));
})();
