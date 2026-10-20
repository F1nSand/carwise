import { describe, expect, it } from 'vitest'
import { parseAgentMarkdown } from './agentMarkdown'

describe('parseAgentMarkdown', () => {
  it('parses headings, paragraphs, lists, quotes and tables', () => {
    const blocks = parseAgentMarkdown(
      '# 标题\n\n说明文字\n\n- 条目一\n- 条目二\n\n1. 第一步\n2. 第二步\n\n> 边界说明\n\n| 列 | 数值 |\n| --- | --- |\n| A | 1 |',
    )

    expect(blocks).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'heading', level: 1 }),
      expect.objectContaining({ type: 'paragraph' }),
      expect.objectContaining({ type: 'unorderedList' }),
      expect.objectContaining({ type: 'orderedList' }),
      expect.objectContaining({ type: 'quote' }),
      expect.objectContaining({ type: 'table' }),
    ]))
  })

  it('parses inline emphasis and code tokens', () => {
    const [block] = parseAgentMarkdown('**重点**、*强调*和 `code`')

    expect(block).toMatchObject({
      type: 'paragraph',
      tokens: [
        { type: 'strong', value: '重点' },
        { type: 'text', value: '、' },
        { type: 'emphasis', value: '强调' },
        { type: 'text', value: '和 ' },
        { type: 'code', value: 'code' },
      ],
    })
  })

  it('keeps HTML-like input as text instead of executable markup', () => {
    expect(parseAgentMarkdown('<script>alert(1)</script>')).toEqual([
      {
        type: 'paragraph',
        tokens: [{ type: 'text', value: '<script>alert(1)</script>' }],
      },
    ])
  })
})
