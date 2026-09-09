import type { Draft, Order } from "./types";

type Props = {
  order: Order | null;
  draft: Draft | null;
  loading: boolean;
  error: string;
  onClose: () => void;
};

export default function OrderDetailsDrawer({ order, draft, loading, error, onClose }: Props) {
  if (!order) return null;

  return <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => {
    if (event.target === event.currentTarget) onClose();
  }}>
    <aside className="order-drawer" role="dialog" aria-modal="true" aria-labelledby="order-detail-title">
      <div className="drawer-header">
        <div><span>订单详情</span><h3 id="order-detail-title">{order.order_no}</h3></div>
        <button type="button" aria-label="关闭订单详情" onClick={onClose}>×</button>
      </div>
      {loading && <p className="drawer-state">正在读取完整订单…</p>}
      {error && <p className="inline-warning" role="alert">{error}</p>}
      <div className="order-facts">
        <article><span>客户</span><strong>{order.customer_name}</strong></article>
        <article><span>订单状态</span><strong>{order.status}</strong></article>
        <article><span>站点 / 目录</span><strong>{order.site_id ?? "-"} / v{order.sku_version ?? "-"}</strong></article>
        <article><span>创建时间</span><strong>{new Date(order.created_at).toLocaleString("zh-CN")}</strong></article>
      </div>
      <div className="drawer-table table-wrap"><table><thead><tr><th>商品</th><th>数量</th><th>单位</th><th>单价</th><th>小计</th></tr></thead><tbody>
        {order.items.map((item) => <tr key={item.line_no}>
          <td><strong>{item.product_name}</strong>{item.note && <small>{item.note}</small>}</td>
          <td>{item.quantity}</td><td>{item.unit}</td>
          <td>¥{item.unit_price.toFixed(2)}</td><td>¥{item.subtotal.toFixed(2)}</td>
        </tr>)}
      </tbody></table></div>
      <div className="drawer-total"><span>{order.items.length} 项商品</span><strong>合计 ¥{order.total_amount.toFixed(2)}</strong></div>
      <section className="source-evidence">
        <h4>来源与证据</h4>
        <dl>
          <div><dt>输入来源</dt><dd>{order.source_filename || order.input_type || "文本输入"}</dd></div>
          <div><dt>订单原话</dt><dd>{order.source_text || "-"}</dd></div>
          <div><dt>草稿 ID</dt><dd>{order.draft_id}</dd></div>
          {draft && <div><dt>召回证据</dt><dd>{draft.retrieval.length ? `${draft.retrieval.length} 条本地目录候选记录` : "精确匹配，无额外候选"}</dd></div>}
        </dl>
      </section>
    </aside>
  </div>;
}
