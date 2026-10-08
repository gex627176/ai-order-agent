import { useEffect, useState } from "react";
import { api } from "./api";
import type { AliasCandidate, BusinessMetrics, Product } from "./types";

type Props = {
  products: Product[];
  onProductsChanged: (products: Product[]) => void;
  onMessage: (message: string, error?: boolean) => void;
};

const EMPTY_PRODUCT: Omit<Product, "id"> = {
  sku: "", name: "", aliases: [], unit: "斤", unit_price: 0, active: true,
};

export default function CatalogPanel({ products, onProductsChanged, onMessage }: Props) {
  const [editing, setEditing] = useState<Record<number, Product>>({});
  const [newProduct, setNewProduct] = useState(EMPTY_PRODUCT);
  const [candidates, setCandidates] = useState<AliasCandidate[]>([]);
  const [metrics, setMetrics] = useState<BusinessMetrics | null>(null);
  const [busy, setBusy] = useState(false);
  const [governanceError, setGovernanceError] = useState("");

  useEffect(() => { void refreshGovernance(); }, []);

  async function refreshGovernance() {
    const [candidateResult, metricResult] = await Promise.allSettled([
      api.aliasCandidates(), api.businessMetrics(),
    ]);
    if (candidateResult.status === "fulfilled") setCandidates(candidateResult.value);
    if (metricResult.status === "fulfilled") setMetrics(metricResult.value);
    const failed = [candidateResult, metricResult].filter((result) => result.status === "rejected").length;
    setGovernanceError(failed ? "部分治理数据暂时不可用，不影响录单和目录维护。" : "");
  }

  async function refreshProducts() {
    onProductsChanged(await api.products());
    await refreshGovernance();
  }

  function current(product: Product) { return editing[product.id] ?? product; }

  function patchProduct(product: Product, patch: Partial<Product>) {
    setEditing((value) => ({ ...value, [product.id]: { ...current(product), ...patch } }));
  }

  async function saveProduct(product: Product) {
    setBusy(true);
    try {
      const result = await api.updateProduct(current(product));
      setEditing((value) => { const next = { ...value }; delete next[product.id]; return next; });
      await refreshProducts();
      onMessage(
        result.index_synced
          ? `商品已保存并发布 SKU v${result.version.version}`
          : `${result.index_error}（SKU v${result.version.version}）`,
        !result.index_synced,
      );
    } catch (reason) {
      onMessage(reason instanceof Error ? reason.message : "商品保存失败", true);
    } finally { setBusy(false); }
  }

  async function addProduct() {
    setBusy(true);
    try {
      const result = await api.createProduct(newProduct);
      setNewProduct(EMPTY_PRODUCT);
      await refreshProducts();
      onMessage(
        result.index_synced
          ? `商品已新增并发布 SKU v${result.version.version}`
          : `${result.index_error}（SKU v${result.version.version}）`,
        !result.index_synced,
      );
    } catch (reason) {
      onMessage(reason instanceof Error ? reason.message : "商品新增失败", true);
    } finally { setBusy(false); }
  }

  async function importCatalog(file: File | undefined) {
    if (!file) return;
    setBusy(true);
    try {
      const result = await api.importCatalog(file);
      await refreshProducts();
      onMessage(
        result.index_synced
          ? `已导入 ${result.imported_count} 条商品并发布 SKU v${result.version.version}`
          : `${result.index_error}（已导入 ${result.imported_count} 条，SKU v${result.version.version}）`,
        !result.index_synced,
      );
    } catch (reason) {
      onMessage(reason instanceof Error ? reason.message : "目录导入失败", true);
    } finally { setBusy(false); }
  }

  async function review(candidate: AliasCandidate, decision: "approved" | "rejected") {
    setBusy(true);
    try {
      const result = await api.reviewAliasCandidate(candidate.id, decision);
      await refreshProducts();
      if (decision === "approved" && !result.index_synced) {
        onMessage(`${result.index_error}（别名“${candidate.alias}”已审核通过）`, true);
      } else {
        onMessage(decision === "approved" ? `别名“${candidate.alias}”已审核通过` : `别名“${candidate.alias}”已拒绝`);
      }
    } catch (reason) {
      onMessage(reason instanceof Error ? reason.message : "别名审核失败", true);
    } finally { setBusy(false); }
  }

  return <>
    <section className="card business-panel">
      <div className="section-title"><span>07</span><div><h3>录单业务指标</h3><p>只统计目录匹配、人工处理和任务表现</p></div></div>
      {governanceError && <p className="inline-warning" role="status">{governanceError}</p>}
      <div className="metric-grid business-metrics">
        <article><span>任务成功率</span><strong>{(metrics?.task_success_rate ?? 0).toFixed(1)}%</strong></article>
        <article><span>自动匹配率</span><strong>{(metrics?.auto_match_rate ?? 0).toFixed(1)}%</strong></article>
        <article><span>未匹配率</span><strong>{(metrics?.unmatched_rate ?? 0).toFixed(1)}%</strong></article>
        <article><span>人工修改率</span><strong>{(metrics?.correction_rate ?? 0).toFixed(1)}%</strong></article>
        <article><span>待审别名</span><strong>{metrics?.pending_alias_candidates ?? 0}</strong></article>
        <article><span>目录版本</span><strong>v{metrics?.current_sku_version ?? 1}</strong></article>
        <article><span>启用商品</span><strong>{metrics?.active_products ?? products.filter((item) => item.active).length}</strong></article>
        <article><span>任务 P95</span><strong>{metrics?.p95_task_duration_ms ?? 0}<small> ms</small></strong></article>
      </div>
    </section>

    <section className="card catalog-panel">
      <div className="section-title"><span>08</span><div><h3>站点商品目录</h3><p>每次保存都会发布新版本，旧草稿继续使用原快照</p></div>
        <label className="catalog-import">导入 CSV / XLSX<input type="file" accept=".csv,.xlsx" disabled={busy} onChange={(event) => { void importCatalog(event.target.files?.[0]); event.target.value = ""; }} /></label>
      </div>
      <div className="table-wrap"><table className="catalog-table"><thead><tr><th>SKU</th><th>商品</th><th>别名（逗号分隔）</th><th>单位</th><th>单价</th><th>启用</th><th>操作</th></tr></thead><tbody>
        {products.map((product) => {
          const item = current(product);
          return <tr key={product.id} className={item.active ? "" : "inactive"}>
            <td><input value={item.sku} onChange={(event) => patchProduct(product, { sku: event.target.value })} /></td>
            <td><input value={item.name} onChange={(event) => patchProduct(product, { name: event.target.value })} /></td>
            <td><input value={item.aliases.join("，")} onChange={(event) => patchProduct(product, { aliases: event.target.value.split(/[,，、]/).map((value) => value.trim()).filter(Boolean) })} /></td>
            <td><input value={item.unit} onChange={(event) => patchProduct(product, { unit: event.target.value })} /></td>
            <td><input type="number" min="0" step="0.01" value={item.unit_price} onChange={(event) => patchProduct(product, { unit_price: Number(event.target.value) })} /></td>
            <td><input className="toggle" type="checkbox" checked={item.active} onChange={(event) => patchProduct(product, { active: event.target.checked })} /></td>
            <td><button disabled={busy || !editing[product.id]} onClick={() => void saveProduct(product)}>保存发布</button></td>
          </tr>;
        })}
        <tr className="new-product">
          <td><input placeholder="SKU-CODE" value={newProduct.sku} onChange={(event) => setNewProduct({ ...newProduct, sku: event.target.value })} /></td>
          <td><input placeholder="商品名称" value={newProduct.name} onChange={(event) => setNewProduct({ ...newProduct, name: event.target.value })} /></td>
          <td><input placeholder="别名1，别名2" value={newProduct.aliases.join("，")} onChange={(event) => setNewProduct({ ...newProduct, aliases: event.target.value.split(/[,，、]/).map((value) => value.trim()).filter(Boolean) })} /></td>
          <td><input value={newProduct.unit} onChange={(event) => setNewProduct({ ...newProduct, unit: event.target.value })} /></td>
          <td><input type="number" min="0" step="0.01" value={newProduct.unit_price} onChange={(event) => setNewProduct({ ...newProduct, unit_price: Number(event.target.value) })} /></td>
          <td><input className="toggle" type="checkbox" checked={newProduct.active} onChange={(event) => setNewProduct({ ...newProduct, active: event.target.checked })} /></td>
          <td><button disabled={busy || !newProduct.sku || !newProduct.name} onClick={() => void addProduct()}>新增发布</button></td>
        </tr>
      </tbody></table></div>
    </section>

    <section className="card alias-panel">
      <div className="section-title"><span>09</span><div><h3>纠错别名审核</h3><p>人工改选只产生候选，审核通过后才进入正式目录</p></div></div>
      {candidates.length === 0 ? <p className="muted">还没有别名候选。</p> : <div className="alias-list">
        {candidates.map((candidate) => <article key={candidate.id}>
          <div><strong>{candidate.alias}</strong><span>建议映射到 {candidate.product_name} · 证据 {candidate.evidence_count} 次</span></div>
          <em className={candidate.status}>{candidate.status === "pending" ? "待审核" : candidate.status === "approved" ? "已通过" : "已拒绝"}</em>
          {candidate.status === "pending" && <div className="alias-actions"><button disabled={busy} onClick={() => void review(candidate, "approved")}>通过</button><button disabled={busy} onClick={() => void review(candidate, "rejected")}>拒绝</button></div>}
        </article>)}
      </div>}
    </section>
  </>;
}
