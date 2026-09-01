export interface MindmapNode {
  id: string;
  title: string;
  level: number;
  children: MindmapNode[];
}

export interface MindmapArtifact {
  raw: string;
  title: string;
  nodes: MindmapNode[];
}

export interface PositionedNode extends MindmapNode {
  x: number;
  y: number;
  depth: number;
  hasChildren: boolean;
  collapsed: boolean;
}

export interface MindmapEdge {
  from: PositionedNode;
  to: PositionedNode;
}

export interface MindmapLayout {
  nodes: PositionedNode[];
  edges: MindmapEdge[];
  width: number;
  height: number;
}

export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 72;
export const LEVEL_GAP = 290;
export const ROW_GAP = 30;
export const CANVAS_PADDING = 72;
export const CANVAS_RIGHT_PADDING = 220;
export const CANVAS_BOTTOM_PADDING = 140;
export const GRAPH_PANEL_FIT_WIDTH = 980;
export const GRAPH_PANEL_FIT_HEIGHT = 660;

function cleanMindmapTitle(value: string) {
  return value
    .replace(/\[(.*?)\]\(.*?\)/g, "$1")
    .replace(/[`*_#]/g, "")
    .trim();
}

function makeNode(id: string, title: string, level: number): MindmapNode {
  return {
    id,
    title: cleanMindmapTitle(title),
    level,
    children: [],
  };
}

export function parseMindmap(raw: string): MindmapArtifact {
  const virtualRoot: MindmapNode = {
    id: "root",
    title: "root",
    level: 0,
    children: [],
  };
  const stack: MindmapNode[] = [virtualRoot];
  const bulletStack: Array<{ indent: number; node: MindmapNode }> = [];
  let sequence = 0;
  let hasHeading = false;

  raw.split(/\r?\n/).forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed) return;

    const headingMatch = trimmed.match(/^(#{1,6})\s+(.+)$/);
    const bulletMatch = line.match(/^(\s*)[-*+]\s+(.+)$/);
    let level = 1;
    let title = trimmed;

    if (headingMatch) {
      level = headingMatch[1].length;
      title = headingMatch[2];
      hasHeading = true;
      bulletStack.length = 0;
    } else if (bulletMatch) {
      const indent = bulletMatch[1].replace(/\t/g, "  ").length;
      while (bulletStack.length && indent <= bulletStack[bulletStack.length - 1].indent) {
        bulletStack.pop();
      }
      const parentLevel = bulletStack.length
        ? bulletStack[bulletStack.length - 1].node.level
        : stack[stack.length - 1]?.level ?? 0;
      level = hasHeading ? parentLevel + 1 : Math.floor(indent / 2) + 1;
      title = bulletMatch[2];
    }

    const node = makeNode(`mindmap-node-${sequence}`, title, level);
    sequence += 1;

    while (stack.length > 1 && stack[stack.length - 1].level >= node.level) {
      stack.pop();
    }

    stack[stack.length - 1].children.push(node);
    stack.push(node);
    if (bulletMatch) {
      const indent = bulletMatch[1].replace(/\t/g, "  ").length;
      bulletStack.push({ indent, node });
    }
  });

  if (!virtualRoot.children.length) {
    virtualRoot.children.push({
      id: "mindmap-node-empty",
      title: "未生成可视化节点",
      level: 1,
      children: [],
    });
  }

  return {
    raw,
    title: virtualRoot.children[0]?.title || "研究产物",
    nodes: virtualRoot.children,
  };
}

export function buildMindmapLayout(nodes: MindmapNode[], collapsedNodeIds: Set<string>): MindmapLayout {
  const positionedNodes: PositionedNode[] = [];
  const edges: MindmapEdge[] = [];
  let leafCursor = 0;

  const walk = (node: MindmapNode, depth: number): PositionedNode => {
    const collapsed = collapsedNodeIds.has(node.id);
    const visibleChildren = collapsed ? [] : node.children;
    const childPositions = visibleChildren.map((child) => walk(child, depth + 1));

    const y = childPositions.length
      ? (childPositions[0].y + childPositions[childPositions.length - 1].y) / 2
      : leafCursor++ * (NODE_HEIGHT + ROW_GAP);

    const positioned: PositionedNode = {
      ...node,
      x: depth * LEVEL_GAP,
      y,
      depth,
      hasChildren: node.children.length > 0,
      collapsed,
    };

    positionedNodes.push(positioned);
    childPositions.forEach((child) => edges.push({ from: positioned, to: child }));
    return positioned;
  };

  nodes.forEach((node) => walk(node, 0));
  const maxX = positionedNodes.reduce((current, node) => Math.max(current, node.x), 0);
  const maxY = positionedNodes.reduce((current, node) => Math.max(current, node.y), 0);

  return {
    nodes: positionedNodes,
    edges,
    width: maxX + NODE_WIDTH + CANVAS_PADDING + CANVAS_RIGHT_PADDING,
    height: Math.max(420, maxY + NODE_HEIGHT + CANVAS_PADDING + CANVAS_BOTTOM_PADDING),
  };
}

export function graphFitScale(layout: MindmapLayout) {
  const widthScale = GRAPH_PANEL_FIT_WIDTH / Math.max(layout.width, 1);
  const heightScale = GRAPH_PANEL_FIT_HEIGHT / Math.max(layout.height, 1);
  return Math.max(0.26, Math.min(0.82, widthScale, heightScale));
}

export function edgePath(edge: MindmapEdge) {
  const startX = edge.from.x + CANVAS_PADDING + NODE_WIDTH;
  const startY = edge.from.y + CANVAS_PADDING + NODE_HEIGHT / 2;
  const endX = edge.to.x + CANVAS_PADDING;
  const endY = edge.to.y + CANVAS_PADDING + NODE_HEIGHT / 2;
  const middle = Math.max(88, (endX - startX) * 0.55);

  return `M ${startX} ${startY} C ${startX + middle} ${startY}, ${endX - middle} ${endY}, ${endX} ${endY}`;
}

export function formatMindmapBranch(node: MindmapNode): string {
  const lines: string[] = [];

  const walk = (current: MindmapNode, depth: number) => {
    const prefix = depth === 0 ? "" : `${"  ".repeat(depth - 1)}- `;
    lines.push(`${prefix}${current.title}`);
    current.children.forEach((child) => walk(child, depth + 1));
  };

  walk(node, 0);
  return lines.join("\n");
}
