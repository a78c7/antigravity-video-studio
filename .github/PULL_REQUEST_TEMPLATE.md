## 变更说明

<!-- 请简述本次 PR 解决的问题、新增能力或修复内容 -->

- 关联 Issue：`#`（如有）
- 变更类型：
  - [ ] Bug 修复（bug fix）
  - [ ] 新功能（feature）
  - [ ] 文档 / 脚本 / CI 优化（docs / chore）

## 改动要点

-

## 验证与自检清单

- [ ] 已运行 `python3 scripts/privacy_check.py` 且结果 0 命中（无明文密钥、私网 IP、本机绝对路径或敏感追踪文件）
- [ ] 已运行 `pytest -q tests/test_smoke.py` 全部通过
- [ ] 未提交 `.env`、`.secrets.env`、`.privacy-blocklist.local` 或 `data/` 运行时文件
- [ ] 如改动前端源码（`frontend/`），已执行 `npm run build` 并确认构建无报错
- [ ] 如改动启动脚本，已验证 `bash -n start.sh` 或 Windows PowerShell 语法解析
- [ ] 遵守「0 封号红线」（不涉及自动化操控 `flow.google.com` 或第三方平台自动发评）及 AGPL-3.0 开源协议要求
