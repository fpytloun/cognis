import { blockChildren, blockType, type RichBlock } from '$lib/rich-deliverable';

export function richScenarioBlocks(blocks: RichBlock[]): RichBlock[] {
  const result: RichBlock[] = [];
  const visit = (block: RichBlock) => {
    result.push(block);
    for (const child of blockChildren(block)) visit(child);
  };
  for (const block of blocks) visit(block);
  return result;
}

export function findRichScenarioBlock(
  blocks: RichBlock[],
  requestedType: string,
  occurrence = 0,
): RichBlock | null {
  let seen = 0;
  for (const block of richScenarioBlocks(blocks)) {
    if (blockType(block) !== requestedType) continue;
    if (seen === occurrence) return block;
    seen += 1;
  }
  return null;
}
