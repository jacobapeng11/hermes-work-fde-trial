/* 侧栏导航自定义图标（014 §13）：用户提供的 heroicons 风格线框 SVG，
   替代 lucide BrainCircuit（技能）与 Sparkles（专家）。
   签名兼容 SidebarView.icon 的 ComponentType<{ size?: string | number }>。 */

interface NavIconProps {
  size?: string | number;
}

export function SkillsNavIcon({ size = 16 }: NavIconProps) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 4a2 2 0 114 0v1a1 1 0 001 1h3a1 1 0 011 1v3a1 1 0 01-1 1h-1a2 2 0 100 4h1a1 1 0 011 1v3a1 1 0 01-1 1h-3a1 1 0 01-1-1v-1a2 2 0 10-4 0v1a1 1 0 01-1 1H7a1 1 0 01-1-1v-3a1 1 0 00-1-1H4a2 2 0 110-4h1a1 1 0 001-1V7a1 1 0 011-1h3a1 1 0 001-1V4z"/>
  </svg>;
}

export function ExpertsNavIcon({ size = 16 }: NavIconProps) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12a4 4 0 100-8 4 4 0 000 8zM3 20a6 6 0 0112 0M19 8l1.5 3.5L24 12l-3.5 1.5L19 17l-1.5-3.5L14 12l3.5-1.5L19 8z"/>
  </svg>;
}
