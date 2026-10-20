export type MarkdownInlineToken = {
  type: 'text' | 'strong' | 'emphasis' | 'code'
  value: string
}

export type MarkdownBlock = {
  type: 'heading' | 'paragraph' | 'unorderedList' | 'orderedList' | 'quote' | 'table'
  level?: 1 | 2 | 3
  tokens?: MarkdownInlineToken[]
  items?: MarkdownInlineToken[][]
  headers?: MarkdownInlineToken[]
  rows?: MarkdownInlineToken[][][]
}

function parseInline(source: string): MarkdownInlineToken[] {
  const tokens: MarkdownInlineToken[] = []
  const pattern = /(\*\*|__)(.+?)\1|`([^`\n]+)`|(\*|_)([^*_\n]+?)\4/g
  let cursor = 0

  for (const match of source.matchAll(pattern)) {
    const index = match.index ?? 0
    if (index > cursor) tokens.push({ type: 'text', value: source.slice(cursor, index) })
    if (match[2] !== undefined) tokens.push({ type: 'strong', value: match[2] })
    else if (match[3] !== undefined) tokens.push({ type: 'code', value: match[3] })
    else if (match[5] !== undefined) tokens.push({ type: 'emphasis', value: match[5] })
    cursor = index + match[0].length
  }

  if (cursor < source.length) tokens.push({ type: 'text', value: source.slice(cursor) })
  return tokens.length ? tokens : [{ type: 'text', value: source }]
}

function isTableSeparator(line: string): boolean {
  const cells = line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|')
  return cells.length > 0 && cells.every((cell) => /^\s*:?-{3,}:?\s*$/.test(cell))
}

function tableCells(line: string): string[] {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim())
}

function isBlockStart(line: string): boolean {
  return /^(?:\s{0,3}#{1,3}\s+|\s*[-*•]\s+|\s*\d+\.\s+|\s*>\s?)/.test(line)
}

export function parseAgentMarkdown(source: string): MarkdownBlock[] {
  const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n')
  const blocks: MarkdownBlock[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index].trimEnd()
    if (!line.trim()) {
      index += 1
      continue
    }

    const heading = line.match(/^\s{0,3}(#{1,3})\s+(.+?)\s*#*\s*$/)
    if (heading) {
      blocks.push({ type: 'heading', level: heading[1].length as 1 | 2 | 3, tokens: parseInline(heading[2]) })
      index += 1
      continue
    }

    if (line.includes('|') && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      const headers = tableCells(line).map(parseInline)
      index += 2
      const rows: MarkdownInlineToken[][][] = []
      while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
        rows.push(tableCells(lines[index]).map(parseInline))
        index += 1
      }
      blocks.push({ type: 'table', headers, rows })
      continue
    }

    const unordered = line.match(/^\s*[-*•]\s+(.+)$/)
    if (unordered) {
      const items: MarkdownInlineToken[][] = [parseInline(unordered[1])]
      index += 1
      while (index < lines.length) {
        const next = lines[index].match(/^\s*[-*•]\s+(.+)$/)
        if (!next) break
        items.push(parseInline(next[1]))
        index += 1
      }
      blocks.push({ type: 'unorderedList', items })
      continue
    }

    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/)
    if (ordered) {
      const items: MarkdownInlineToken[][] = [parseInline(ordered[1])]
      index += 1
      while (index < lines.length) {
        const next = lines[index].match(/^\s*\d+[.)]\s+(.+)$/)
        if (!next) break
        items.push(parseInline(next[1]))
        index += 1
      }
      blocks.push({ type: 'orderedList', items })
      continue
    }

    const quote = line.match(/^\s*>\s?(.*)$/)
    if (quote) {
      const quoteLines = [quote[1]]
      index += 1
      while (index < lines.length) {
        const next = lines[index].match(/^\s*>\s?(.*)$/)
        if (!next) break
        quoteLines.push(next[1])
        index += 1
      }
      blocks.push({ type: 'quote', tokens: parseInline(quoteLines.join(' ')) })
      continue
    }

    const paragraphLines = [line.trim()]
    index += 1
    while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index])) {
      paragraphLines.push(lines[index].trim())
      index += 1
    }
    blocks.push({ type: 'paragraph', tokens: parseInline(paragraphLines.join(' ')) })
  }

  return blocks
}
