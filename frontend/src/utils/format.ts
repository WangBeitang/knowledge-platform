/** 通用格式化工具（骨架）。后续阶段补充枚举文案映射等。 */

/** ISO 8601 → 本地可读时间；空值返回 '--' */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '--'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '--'
  return d.toLocaleString('zh-CN', { hour12: false })
}
