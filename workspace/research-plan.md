# Research Plan: Anthill 项目市场前景分析

## Research Question
Anthill（基于信息素涌现机制的多智能体框架）在市场中有多大的生存空间？
主流 AI 平台的 agent 能力是否会覆盖/挤压它的差异化价值？

## Intended Audience
项目作者/贡献者，需要判断是否值得继续投入。

## Constraints
- Freshness: 2025-2026 年市场动态
- Geography: 全球市场，重点看开源社区和开发者工具生态
- Output type: **Brief memo** (1500-2500 words)
- Stakes: Medium — 影响项目资源分配决策

## Why This Skill Is Justified
- 需要跨多个信息源（竞品、市场数据、技术趋势）的综合分析
- 需要可复用的证据制品
- 决策导向，需明确 trade-off

## Orchestration Mode
**Lead + subagents** — 4 个独立研究线程，适合并行：
- Thread A: 多智能体框架竞品格局
- Thread B: 主流 AI 平台的 agent 能力
- Thread C: 市场趋势与采用模式
- Thread D (lead): Anthill 差异化定位分析（lead 自己完成）

## Research Threads

### Thread A: 多智能体框架竞品格局
- Objective: 摸清 CrewAI, AutoGen, LangGraph, MetaGPT, Agency 等框架的定位、社区规模、融资情况
- Starting queries: "multi-agent framework 2025 comparison", "CrewAI vs AutoGen vs LangGraph 2025", "multi-agent AI framework market 2026"
- Done when: 列出 5+ 竞品及其规模/定位/差异化

### Thread B: 主流 AI 平台的 agent 能力
- Objective: OpenAI, Anthropic, Google, 国内主流平台的 agent/custom GPT/多智能体能力
- Starting queries: "OpenAI agents platform 2025", "Anthropic Claude agent capabilities", "Google AI agent builder", "主流AI平台 agent 能力 2026"
- Done when: 清晰描述 3-5 个主流平台的 agent 策略和对开源框架的挤压路径

### Thread C: 市场趋势与采用模式
- Objective: 开发者采用开源 agent 框架的趋势、GitHub star 增长、企业级需求
- Starting queries: "open source AI agent framework adoption 2025 2026", "agent framework market size growth", "AI agent developer survey 2025"
- Done when: 有数据支撑的市场规模和增长判断

### Thread D (lead): Anthill 差异化定位
- Objective: 基于 A/B/C 结果，评估 Anthill 的生存空间和推荐路径
- Done when: 给出 3 个可能的走向判断
