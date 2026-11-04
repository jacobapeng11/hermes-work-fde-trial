# 根 tests 目录职责

本目录只存放正式的测试、验收和验证产物。这里不是测试代码的默认集中
位置，也不再维护 `e2e/`、`fixtures/` 等常驻占位子目录。

## 当前规则

1. 测试代码按业务模块进入对应模块测试目录：
   - Backend：`backend/tests/unit/`、`backend/tests/integration/`
   - Runtime Supervisor：`runtime/supervisor/tests/`
   - User Web E2E：`frontend/user-web/e2e/`
   - Enterprise Knowledge：`enterprise-knowledge/tests/`
   - Desktop：`cowork-hermes-desktop/tests/`
2. 根 `tests/` 中的正式测试、验收、验证类 Markdown 产物使用：

   ```text
   {YYYY-MM-DD}-{NNN}-{需求名称}.md
   ```

   对应资源目录使用去掉 `.md` 后的完全同名目录。
3. 研发过程中的测试实验、调查档案和未成为正式验收结论的过程记录，继续
   归档在 `docs/superpowers/tests/`，不放入本目录。
4. 跨模块测试 fixture 不默认集中放到根 `tests/fixtures/`；应放入对应模块
   的明确业务 fixture 目录，并遵守该模块最近一层 `AGENTS.md`。

## 使用判断

- 会作为长期正式证据保留的验收报告、验证记录和配套资源：放本目录。
- 会被 pytest、Vitest、Playwright 或其它测试入口直接执行的代码：放对应
  模块测试目录。
- 过程实验、失败调查、临时截图或未定论记录：放 `docs/superpowers/tests/`
  对应日期型产物目录。
