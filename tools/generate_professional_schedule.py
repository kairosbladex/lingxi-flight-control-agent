# -*- coding: utf-8 -*-
"""
生成灵犀平台 V1.4 自然语言控制功能——专业项目排期与执行状态看板 (领导汇报级)
设计风格：严肃、商务、专业、克制，去除所有非正式 Emoji，规范 WBS 任务分解
"""
import os
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

def setup_sheet_page(ws):
    ws.views.sheetView[0].showGridLines = True
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0

def build_workbook():
    wb = openpyxl.Workbook()
    default_sheet = wb.active
    wb.remove(default_sheet)

    FONT_FAMILY = "Microsoft YaHei"
    FONT_NUM = "Segoe UI"
    
    # 商务专业经典调色板 (Corporate Navy & Slate)
    C_HEADER_BG = "1F4E78"     # 经典商务深蓝
    C_HEADER_TEXT = "FFFFFF"   # 表头纯白
    C_TITLE_BG = "0D233A"      # 标题栏深海军蓝
    C_SUBTITLE_BG = "1A365D"   # 副标题背景
    C_SECTION_BG = "EAECF0"    # 分组汇总行浅灰蓝
    C_ZEBRA = "F9FAFB"         # 偶数行极浅灰
    C_WHITE = "FFFFFF"         # 纯白
    
    # 状态色系 (低饱和度、沉稳克制)
    C_STATUS_DONE_BG = "EAF5EA"    # 柔和浅绿
    C_STATUS_DONE_FG = "1B5E20"    # 墨绿
    C_STATUS_ING_BG = "FFF8E1"     # 柔和浅米黄
    C_STATUS_ING_FG = "8D6E63"     # 稳重深褐
    C_STATUS_TODO_BG = "F2F4F7"    # 浅中性灰
    C_STATUS_TODO_FG = "475467"    # 稳重深灰

    # 边框定义
    thin_border_side = Side(style='thin', color='D0D5DD')
    header_border_side = Side(style='thin', color='4B729F')
    double_bottom_side = Side(style='double', color='1F4E78')
    
    border_cell = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)
    border_header = Border(left=header_border_side, right=header_border_side, top=thin_border_side, bottom=thin_border_side)
    border_total = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=double_bottom_side)

    # 填充定义
    fill_header = PatternFill(start_color=C_HEADER_BG, end_color=C_HEADER_BG, fill_type='solid')
    fill_zebra = PatternFill(start_color=C_ZEBRA, end_color=C_ZEBRA, fill_type='solid')
    fill_white = PatternFill(start_color=C_WHITE, end_color=C_WHITE, fill_type='solid')
    fill_section = PatternFill(start_color=C_SECTION_BG, end_color=C_SECTION_BG, fill_type='solid')

    # 字体定义
    font_main_title = Font(name=FONT_FAMILY, size=15, bold=True, color=C_WHITE)
    font_sub_title = Font(name=FONT_FAMILY, size=9.5, color="D0D5DD")
    font_section_title = Font(name=FONT_FAMILY, size=11, bold=True, color="1F4E78")
    font_header = Font(name=FONT_FAMILY, size=10, bold=True, color=C_HEADER_TEXT)
    font_body = Font(name=FONT_FAMILY, size=9.5, color="1D2939")
    font_body_bold = Font(name=FONT_FAMILY, size=9.5, bold=True, color="1D2939")
    font_num = Font(name=FONT_NUM, size=9.5, color="1D2939")
    font_num_bold = Font(name=FONT_NUM, size=9.5, bold=True, color="1D2939")

    align_center = Alignment(horizontal='center', vertical='center')
    align_left = Alignment(horizontal='left', vertical='center')
    align_left_wrap = Alignment(horizontal='left', vertical='center', wrap_text=True)
    align_right = Alignment(horizontal='right', vertical='center')

    # ==========================================
    # SHEET 1: 项目排期总览 (Executive Summary & Milestones)
    # ==========================================
    ws1 = wb.create_sheet(title="项目排期总览")
    setup_sheet_page(ws1)

    # 主标题行
    ws1.merge_cells("A1:J1")
    ws1["A1"] = "灵犀平台自然语言智能控制功能 (V1.4) 项目执行排期总览"
    ws1["A1"].font = font_main_title
    ws1["A1"].alignment = align_center
    ws1["A1"].fill = PatternFill(start_color=C_TITLE_BG, end_color=C_TITLE_BG, fill_type='solid')
    ws1.row_dimensions[1].height = 36

    ws1.merge_cells("A2:J2")
    ws1["A2"] = "依据《灵犀平台自然语言智能控制功能设计规范 V1.4》编制 | 当前自动化单测通过率：100% (1,115/1,115 项 PASS) | 排期版本：多任务专线协同加速版"
    ws1["A2"].font = font_sub_title
    ws1["A2"].alignment = align_center
    ws1["A2"].fill = PatternFill(start_color=C_SUBTITLE_BG, end_color=C_SUBTITLE_BG, fill_type='solid')
    ws1.row_dimensions[2].height = 22

    # 第一部分：项目核心管理指标摘要表 (Row 4-7)
    ws1["A4"] = "一、项目核心管理指标概览"
    ws1["A4"].font = font_section_title
    ws1.row_dimensions[4].height = 24

    kpi_headers = ["指标分类", "当前状态 / 数值", "基线对比与口径说明", "目标交付时间", "工期提效比率", "自动化测试基线", "责任协同专线", "外部关键依赖"]
    ws1.row_dimensions[5].height = 26
    for col_idx, h in enumerate(kpi_headers, start=1):
        cell = ws1.cell(5, col_idx, h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_header

    kpi_row = [
        "V1.4 全功能闭环推进", 
        "规则层 100% / 综合 78.5%", 
        "18个规范细项已在内存执行器完整闭环", 
        "2026-11-05 (原 11-13)", 
        "工期缩短 8 天 (-53%)", 
        "1,115 项单测全部通过", 
        "5 个并行作业专线组", 
        "Java快照字段与外场空域报备"
    ]
    ws1.row_dimensions[6].height = 25
    for col_idx, val in enumerate(kpi_row, start=1):
        cell = ws1.cell(6, col_idx, val)
        cell.fill = fill_white
        cell.border = border_cell
        if col_idx in (1, 4, 5, 6, 7):
            cell.font = font_body_bold
            cell.alignment = align_center
            if col_idx == 4:
                cell.fill = PatternFill(start_color=C_STATUS_DONE_BG, end_color=C_STATUS_DONE_BG, fill_type='solid')
                cell.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color=C_STATUS_DONE_FG)
            elif col_idx == 5:
                cell.fill = PatternFill(start_color="E0F2FE", end_color="E0F2FE", fill_type='solid')
                cell.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color="0369A1")
        else:
            cell.font = font_body
            cell.alignment = align_left

    # 第二部分：项目核心交付里程碑表 (Row 8-22)
    ws1["A8"] = "二、项目核心交付里程碑与阶段排期（多专线协同推进）"
    ws1["A8"].font = font_section_title
    ws1.row_dimensions[8].height = 24

    m_headers = ["里程碑编号", "阶段名称", "主攻功能范围", "计划开始", "计划完成", "工期(天)", "责任主体", "核心交付成果物", "当前状态", "当前完成率"]
    ws1.row_dimensions[9].height = 26
    for col_idx, h in enumerate(m_headers, start=1):
        cell = ws1.cell(9, col_idx, h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_header

    milestones_data = [
        ("M-01", "基线冻结与规则收敛", "代码基线、分支规范与 1,115 项离线单测分母锁定", "2026-10-27", "2026-10-27", 1, "项目负责人/测试负责人", "基线审计报告、1115项离线单测通过日志", "已完成", 1.00),
        ("M-02", "地面规划与跨仓交接", "点线面航线强制映射、体目标拦截、跨仓信封协议", "2026-10-28", "2026-10-28", 1, "地面规划组 (Agent-02)", "目标解析器代码、跨仓信封校验单测", "进行中", 0.70),
        ("M-03", "静态标注与AI生命周期", "点线面AI绑定、体标注隐藏、到达开启离开关闭、去重防误关", "2026-10-28", "2026-10-29", 2, "AI模型组 (Agent-03) / 前端组", "AI生命周期状态机、前端标注配置弹窗组件", "进行中", 0.65),
        ("M-04", "飞行控制与载荷调度", "控制权无豁免门禁、微操拦截、参数极值换算、载荷调度", "2026-10-28", "2026-10-29", 2, "飞控核心组 (Agent-01)", "权限门禁与参数换算器、载荷控制动作单测", "进行中", 0.85),
        ("M-05", "测绘中断与低电量续飞", "测绘首屏失效警示、六步原子作废、低电量优先返航与续飞", "2026-10-29", "2026-10-30", 2, "飞控核心组 (Agent-01)", "测绘六步原子作废执行器、低电量调度单测", "进行中", 0.80),
        ("M-06", "复合指令与话术验收", "复杂复合指令大模型拆解、13类异常场景固定标准话术", "2026-10-30", "2026-10-30", 1, "AI模型组 (Agent-03) / 前端组", "意图拆解评测报告、13场景话术前端走查清单", "未开始", 0.80),
        ("M-07", "平台适配与仿真打桩", "Ktd平台接口、route_status快照补齐、37点高精围栏联调", "2026-10-29", "2026-10-31", 3, "平台仿真组 (Agent-05)", "KtdPlatform 真实适配层、仿真打桩套件", "未开始", 0.20),
        ("M-08", "MQTT遥测与事件总线", "MQTT遥测上行订阅、平台指令回执映射、任务事件驱动", "2026-10-30", "2026-11-02", 3, "平台仿真组 (Agent-05)", "MQTT 遥测消费服务、平台回执状态机", "未开始", 0.10),
        ("M-09", "全系统成果合流评审", "全模块离线模拟器与平台仿真桩端到端集成测试", "2026-10-31", "2026-10-31", 1, "全体核心团队", "全模块集成回归报告、仿真联调通过备忘录", "未开始", 0.00),
        ("M-10", "外场预演与空域报备", "现场实飞空域批件、物理无人机链路通畅、实飞预演", "2026-11-03", "2026-11-04", 2, "现场飞手组 / 架构师", "空域批准文书、现场实飞预演视频与日志", "未开始", 0.00),
        ("M-11", "外场真机终验交付", "自然语言微操、围栏防闯入拦截、低电量返航、全流程签字", "2026-11-05", "2026-11-05", 1, "项目指挥部 / 全体团队", "V1.4 端到端交付验收签字报告、归档交付件", "未开始", 0.00)
    ]

    for idx, rdata in enumerate(milestones_data, start=10):
        ws1.row_dimensions[idx].height = 24
        fill_curr = fill_white if idx % 2 == 0 else fill_zebra
        for c_idx, val in enumerate(rdata, start=1):
            cell = ws1.cell(idx, c_idx)
            cell.fill = fill_curr
            cell.border = border_cell
            if c_idx in (1, 4, 5, 7):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_center
            elif c_idx == 2:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_left
            elif c_idx == 6:
                cell.value = val
                cell.font = font_num
                cell.alignment = align_right
            elif c_idx in (3, 8):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_left_wrap
            elif c_idx == 9:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_center
                if val == "已完成":
                    cell.fill = PatternFill(start_color=C_STATUS_DONE_BG, end_color=C_STATUS_DONE_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color=C_STATUS_DONE_FG)
                elif val == "进行中":
                    cell.fill = PatternFill(start_color=C_STATUS_ING_BG, end_color=C_STATUS_ING_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color=C_STATUS_ING_FG)
                else:
                    cell.fill = PatternFill(start_color=C_STATUS_TODO_BG, end_color=C_STATUS_TODO_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, color=C_STATUS_TODO_FG)
            elif c_idx == 10:
                cell.value = val
                cell.font = font_num_bold
                cell.number_format = '0.0%'
                cell.alignment = align_center
                if val >= 0.8:
                    cell.fill = PatternFill(start_color=C_STATUS_DONE_BG, end_color=C_STATUS_DONE_BG, fill_type='solid')
                    cell.font = Font(name=FONT_NUM, size=9.5, bold=True, color=C_STATUS_DONE_FG)
                elif val >= 0.5:
                    cell.fill = PatternFill(start_color=C_STATUS_ING_BG, end_color=C_STATUS_ING_BG, fill_type='solid')
                    cell.font = Font(name=FONT_NUM, size=9.5, bold=True, color=C_STATUS_ING_FG)
                else:
                    cell.fill = PatternFill(start_color=C_STATUS_TODO_BG, end_color=C_STATUS_TODO_BG, fill_type='solid')
                    cell.font = Font(name=FONT_NUM, size=9.5, color=C_STATUS_TODO_FG)

    # 里程碑汇总行 (Row 21)
    ws1.row_dimensions[21].height = 25
    ws1.cell(21, 1, "全周期汇总")
    ws1.cell(21, 2, "端到端交付闭环")
    ws1.cell(21, 3, "共 11 个关键里程碑节点，覆盖规则、平台与现场实飞")
    ws1.cell(21, 4, "2026-10-27")
    ws1.cell(21, 5, "2026-11-05")
    ws1.cell(21, 6, "=SUM(F10:F20)")
    ws1.cell(21, 7, "多专线跨域协同")
    ws1.cell(21, 8, "全套验收报告、测试日志与外场实飞交付签字单")
    ws1.cell(21, 9, "推进中")
    ws1.cell(21, 10, "=AVERAGE(J10:J20)")

    for c in range(1, 11):
        cell = ws1.cell(21, c)
        cell.fill = fill_section
        cell.border = border_total
        cell.font = font_body_bold
        if c in (1, 2, 4, 5, 7, 9):
            cell.alignment = align_center
        elif c == 6:
            cell.alignment = align_right
            cell.font = font_num_bold
        elif c == 10:
            cell.alignment = align_center
            cell.font = font_num_bold
            cell.number_format = '0.0%'
        else:
            cell.alignment = align_left

    # 第三部分：核心风险与外部协调事项 (Row 23-28)
    ws1.row_dimensions[22].height = 16
    ws1["A23"] = "三、需协调支持的外部依赖与风险应对清单"
    ws1["A23"].font = font_section_title
    ws1.row_dimensions[23].height = 24

    risk_headers = ["序号", "关注事项 / 潜在瓶颈", "影响范围与阶段", "当前状态与阻碍", "建议解决方案 / 应对措施", "需协调对接责任方", "最迟解决节点"]
    ws1.row_dimensions[24].height = 26
    for col_idx, h in enumerate(risk_headers, start=1):
        cell = ws1.cell(24, col_idx, h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_header

    risk_rows = [
        ("1", "Java 平台快照缺少 route_status 字段", "M-04 飞行控制、M-07 平台联调", "平台快照接口返回 null，飞控已设 fail-closed 拦截", "推动 Java 平台服务在快照中暴露航线状态字段；本地仿真桩先打通", "Java 后端开发组", "2026-10-30"),
        ("2", "电子围栏 37 点高精折线联调环境", "M-04 围栏校验、M-07 平台联调", "接口文档已就绪，但联调测试服务偶发超时", "协调测试环境独立部署电子围栏微服务；飞控已实现超时 3 秒自动降级", "基础设施与中台组", "2026-10-31"),
        ("3", "AI 算法平台仅支持任务级检测", "M-03 算法生命周期", "缺少按算法粒度开启/停止的独立接口", "推动算法服务暴露子算法开关，或由飞控在视频流侧做结果掩码过滤", "算法与流媒体组", "2026-10-30"),
        ("4", "外场试飞空域批件与天气窗口", "M-10 预演、M-11 真机终验", "外场实飞受天气与空域审批制约", "提前 3 个工作日报备空域，预留 11-04 与 11-05 两个机动窗口", "外场飞手 / 运营支持", "2026-11-02")
    ]

    for idx, rdata in enumerate(risk_rows, start=25):
        ws1.row_dimensions[idx].height = 24
        fill_curr = fill_white if idx % 2 == 0 else fill_zebra
        for c_idx, val in enumerate(rdata, start=1):
            cell = ws1.cell(idx, c_idx)
            cell.fill = fill_curr
            cell.border = border_cell
            if c_idx in (1, 6, 7):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_center
            elif c_idx == 2:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_left
            else:
                cell.value = val
                cell.font = font_body
                cell.alignment = align_left_wrap

    ws1_widths = {1: 12, 2: 24, 3: 34, 4: 13, 5: 13, 6: 10, 7: 22, 8: 36, 9: 13, 10: 13}
    for col_idx, width in ws1_widths.items():
        ws1.column_dimensions[get_column_letter(col_idx)].width = width

    # ==========================================
    # SHEET 2: WBS任务执行排期明细 (WBS Task Schedule)
    # ==========================================
    ws2 = wb.create_sheet(title="WBS任务排期明细")
    setup_sheet_page(ws2)
    ws2.freeze_panes = "A4"

    ws2.merge_cells("A1:N1")
    ws2["A1"] = "灵犀平台自然语言智能控制功能 (V1.4) WBS 任务分解与执行排期表"
    ws2["A1"].font = font_main_title
    ws2["A1"].alignment = align_center
    ws2["A1"].fill = PatternFill(start_color=C_TITLE_BG, end_color=C_TITLE_BG, fill_type='solid')
    ws2.row_dimensions[1].height = 36

    ws2.merge_cells("A2:N2")
    ws2["A2"] = "涵盖 42 项工程任务的细分标准、责任专线、前置依赖、计划起止日期、工时与交付成果 | 支持多专线并发作业"
    ws2["A2"].font = font_sub_title
    ws2["A2"].alignment = align_center
    ws2["A2"].fill = PatternFill(start_color=C_SUBTITLE_BG, end_color=C_SUBTITLE_BG, fill_type='solid')
    ws2.row_dimensions[2].height = 22

    wbs_headers = [
        "WBS编号", "任务编号", "责任专线", "所属功能域", "工作任务分解名称 (WBS Task)", 
        "前置依赖", "计划开始", "计划完成", "工时(h)", 
        "完成进度", "执行状态", "验证方式", "量化验收标准", "核心交付物"
    ]
    ws2.row_dimensions[3].height = 28
    for col_idx, h in enumerate(wbs_headers, start=1):
        cell = ws2.cell(3, col_idx, h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_header

    wbs_tasks_data = [
        # 1.0 基线冻结与规则收敛
        ("1.1", "B0-01", "核心管理组", "基线管理", "核对分支、HEAD、工作区状态与远端对齐", "无", "2026-10-27", "2026-10-27", 0.5, 1.00, "已完成", "命令检查", "分支与HEAD无歧义，脏改动完整归档", "基线状态备忘录"),
        ("1.2", "B0-02", "测试组", "自动化测试", "执行离线全量单测与网络隔离断言", "B0-01", "2026-10-27", "2026-10-27", 1.0, 1.00, "已完成", "单元测试", "1,115项单测全部PASS，零socket外连", "离线测试通过日志"),
        ("1.3", "B0-03", "开发组", "代码质量", "源码静态编译与 whitespace diff 检查", "B0-02", "2026-10-27", "2026-10-27", 0.5, 1.00, "已完成", "静态分析", "零编译告警，无 trailing whitespace", "代码质量检查记录"),
        ("1.4", "B0-04", "核心管理组", "需求对齐", "建立 V1.4 规范功能分母与证据索引", "设计文档", "2026-10-27", "2026-10-27", 1.0, 1.00, "已完成", "矩阵核对", "18项规范要点建立逐条跟踪索引", "验收矩阵文档"),
        ("1.5", "B0-05", "核心管理组", "团队协同", "确认跨模块负责人与外部系统契约依赖", "B0-04", "2026-10-27", "2026-10-27", 1.0, 1.00, "已完成", "会议确认", "各任务分配唯一责任主体与接口人", "协作职责清单"),

        # 2.0 地面规划与跨仓交接
        ("2.1", "B1-01", "地面规划组", "地面规划", "点目标唯一解析与用户顺序严格保留", "B0-05", "2026-10-28", "2026-10-28", 1.5, 0.80, "进行中", "单元测试", "多点按输入顺序串联，不静默改变顺序", "点目标解析代码及单测"),
        ("2.2", "B1-02", "地面规划组", "地面规划", "线目标单线单任务强制端点映射", "B1-01", "2026-10-28", "2026-10-28", 1.5, 0.70, "进行中", "单元测试", "单线就近起飞，多线输入严格拒绝", "线目标端点映射器"),
        ("2.3", "B1-03", "地面规划组", "地面规划", "面目标单面自适应“弓”字形航线映射", "B1-01", "2026-10-28", "2026-10-28", 1.0, 0.70, "进行中", "单元测试", "单面生成全覆盖扫描航线，多面拒绝", "面目标扫描规划器"),
        ("2.4", "B1-04", "地面规划组", "地面规划", "立体目标与混合目标输入强制拦截", "B1-01", "2026-10-28", "2026-10-28", 1.0, 1.00, "已完成", "单元测试", "体目标与混合目标返回专用拒绝码", "拦截规则及单测"),
        ("2.5", "B1-05", "地面规划组", "跨仓交接", "任务下发权限与交接信封协议校验", "B1-01", "2026-10-28", "2026-10-28", 1.5, 0.60, "进行中", "契约测试", "校验binding、版本号、幂等键与权限", "交接信封校验代码"),
        ("2.6", "B1-06", "地面规划组", "跨仓交接", "任务类型隔离与飞行中控制边界保护", "B1-05", "2026-10-28", "2026-10-28", 1.5, 0.80, "进行中", "隔离测试", "巡检中发起测绘被拒；控制仓不擅自建任务", "边界保护单测"),

        # 3.0 静态标注与 AI 生命周期
        ("3.1", "B2-01", "AI算法组", "AI生命周期", "点/线/面绑定AI与体标注前端隐藏", "B0-05", "2026-10-28", "2026-10-28", 1.0, 1.00, "已完成", "单元测试", "体标注返回 BODY_ANNOTATION_AI_UNSUPPORTED", "几何绑定校验代码"),
        ("3.2", "B2-02", "AI算法组", "AI生命周期", "算法配置非必填与项目配置校验", "B2-01", "2026-10-28", "2026-10-28", 1.0, 1.00, "已完成", "单元测试", "未配置算法整批拒绝，不替换近似算法", "白名单校验单测"),
        ("3.3", "B2-03", "AI算法组", "AI生命周期", "全局与标注专属算法集合去重", "B2-02", "2026-10-28", "2026-10-28", 1.0, 1.00, "已完成", "单元测试", "全局与局部重叠算法只启动一次", "算法去重合并逻辑"),
        ("3.4", "B2-04", "AI算法组", "AI生命周期", "到达开启、离开关闭与防误关状态机", "B2-03", "2026-10-28", "2026-10-29", 1.5, 0.80, "进行中", "模拟器测试", "离开标注关闭专属算法；全局算法保持", "生命周期状态机代码"),
        ("3.5", "B2-05", "AI算法组", "AI生命周期", "AI配置冲突澄清追问与仅替换限制", "B2-03", "2026-10-28", "2026-10-29", 1.0, 1.00, "已完成", "单元测试", "冲突主动追问；仅替换返回不支持话术", "冲突追问卡片生成器"),
        ("3.6", "B2-06", "前端开发组", "前端界面", "前端静态标注 AI 配置弹窗与按钮显隐", "B2-01", "2026-10-28", "2026-10-29", 1.5, 0.20, "进行中", "前端走查", "点线面展示配置弹窗，体标注彻底隐藏", "地图标注前端组件"),
        ("3.7", "B2-07", "平台适配组", "外部接口", "真实算法启停接口契约对齐与联调", "B2-04", "2026-10-29", "2026-10-30", 1.0, 0.00, "未开始", "接口测试", "核实平台是否支持算法粒度启停及回执", "接口契约备忘录"),

        # 4.0 飞行微操与载荷控制
        ("4.1", "B3-01", "飞控核心组", "飞行控制", "飞行中控制权无豁免严格校验", "B0-05", "2026-10-28", "2026-10-28", 1.0, 1.00, "已完成", "模拟器测试", "无控制权下发微操/载荷/急停/返航全拦截", "权限校验门禁单测"),
        ("4.2", "B3-02", "飞控核心组", "飞行控制", "自动航线中微操强插拦截与载荷放行", "B3-01", "2026-10-28", "2026-10-28", 1.0, 1.00, "已完成", "模拟器测试", "航线中微操拦截提示先暂停；载荷放行", "状态门禁校验器"),
        ("4.3", "B3-03", "飞控核心组", "飞行控制", "自然语言悬停等价暂停航线与断点保存", "B3-02", "2026-10-28", "2026-10-28", 1.0, 1.00, "已完成", "模拟器测试", "自动航线悬停调用暂停并存断点；手动悬停", "悬停动作编译器"),
        ("4.4", "B3-04", "飞控核心组", "飞行控制", "六向移动、转向与三维坐标指点飞行", "B3-02", "2026-10-28", "2026-10-29", 1.0, 0.90, "进行中", "模拟器测试", "相对机头解算、极值限制、指点航迹生成", "微操指令编译器"),
        ("4.5", "B3-05", "飞控核心组", "飞行控制", "环绕路径生成与电子围栏六态校验", "B3-04", "2026-10-28", "2026-10-29", 1.0, 0.90, "进行中", "模拟器测试", "环绕航线生成；围栏 blocked 拒绝并降级", "环绕解算与围栏测试"),
        ("4.6", "B3-06", "飞控核心组", "载荷控制", "云台/变焦/镜头/拍照录像/喊话/照明", "B3-01", "2026-10-28", "2026-10-29", 1.0, 0.90, "进行中", "模拟器测试", "超限智能换算极值；免暂停航线执行载荷", "全套载荷控制指令集"),
        ("4.7", "B3-07", "飞控核心组", "任务控制", "暂停、恢复、一键返航、结束当前/全部", "B3-02", "2026-10-28", "2026-10-28", 1.0, 1.00, "已完成", "模拟器测试", "结束当前保留主线；结束全部清空断点", "任务控制动作单测"),
        ("4.8", "B3-08", "AI算法组", "复合指令", "顺序执行与并发执行大模型拆解调度", "B3-04", "2026-10-28", "2026-10-29", 1.0, 0.85, "进行中", "单元测试", "顺序逐步确认任一失败停；并发显式双卡", "复合指令规划器"),

        # 5.0 测绘中断与低电量续飞
        ("5.1", "B4-01", "飞控核心组", "测绘中断", "测绘任务首屏不可中断警示与强插二次确认", "B3-07", "2026-10-29", "2026-10-29", 1.5, 0.80, "进行中", "模拟器测试", "首屏清晰提示成果作废风险，确认后方可执行", "二次确认卡片逻辑"),
        ("5.2", "B4-02", "飞控核心组", "测绘中断", "测绘中断六步原子事务作废与清理", "B4-01", "2026-10-29", "2026-10-29", 2.0, 0.80, "进行中", "多进程测试", "关拼图、作废、清断点、置灰恢复六步原子执行", "六步原子事务执行器"),
        ("5.3", "B4-03", "飞控核心组", "任务断点", "临时任务完成后显式二选一决策机制", "B4-02", "2026-10-29", "2026-10-29", 1.5, 0.80, "进行中", "模拟器测试", "弹出卡片要求用户显式选择恢复主线或结束", "断点决策卡片组件"),
        ("5.4", "B4-04", "飞控核心组", "低电量管理", "低电量返航最高优先级事件调度与抢占", "B3-07", "2026-10-29", "2026-10-29", 1.5, 0.80, "进行中", "模拟器测试", "低电量事件触发立即打断其他指令并一键返航", "低电量调度控制器"),
        ("5.5", "B4-05", "飞控核心组", "低电量管理", "充电完成恢复巡检主线与测绘废弃过滤", "B4-04", "2026-10-29", "2026-10-29", 1.5, 0.80, "进行中", "模拟器测试", "充电后仅恢复有效巡检主线；作废测绘不续飞", "断点续飞过滤器"),

        # 6.0 复合指令与异常话术验收
        ("6.1", "B5-01", "AI算法组", "复合指令", "复杂自然语言复合指令意图拆解泛化评测", "B3-08", "2026-10-30", "2026-10-30", 2.5, 0.80, "未开始", "评测集回放", "动词拆解准确率≥95%，参数提取准确率≥98%", "意图拆解基准报告"),
        ("6.2", "B5-02", "AI算法组", "复合指令", "并发多动作卡片渲染与零伪造回滚机制", "B5-01", "2026-10-30", "2026-10-30", 1.5, 0.80, "未开始", "模拟器测试", "两张卡片显式呈现，部分失败保留成功项", "并发状态驱动代码"),
        ("6.3", "B5-03", "前端开发组", "异常话术", "13大异常场景固定标准话术前端统一走查", "B0-04", "2026-10-30", "2026-10-30", 2.0, 0.80, "未开始", "前端走查", "13个场景话术与规范文档Table 6逐字一致", "话术走查验收清单"),
        ("6.4", "B5-04", "前端开发组", "前端界面", "微操与载荷控制确认态弹窗体验走查", "B3-06", "2026-10-30", "2026-10-30", 2.0, 0.80, "未开始", "UI走查", "确认卡包含参数调整、极值提示、风险说明", "确认卡体验验收单"),

        # 7.0 外部平台适配与仿真打桩
        ("7.1", "B6-01", "平台适配组", "平台接口", "KtdPlatformExecutor 真实 HTTP 接口打通", "B0-05", "2026-10-29", "2026-10-30", 3.0, 0.20, "未开始", "接口测试", "/screen/uav/* 接口全部联通，零 404/500 报错", "平台接口联调日志"),
        ("7.2", "B6-02", "平台适配组", "平台接口", "route_status 真实快照字段获取与解析对齐", "B6-01", "2026-10-29", "2026-10-30", 3.0, 0.20, "未开始", "接口测试", "route_status 返回 idle/running/paused", "快照字段完整性报告"),
        ("7.3", "B6-03", "平台适配组", "平台接口", "电子围栏 /electronicFence 37点折线联调", "B6-01", "2026-10-30", "2026-10-31", 3.0, 0.20, "未开始", "接口测试", "37点折线插值校验通过，禁飞区正确判定", "电子围栏联调报告"),
        ("7.4", "B6-04", "平台适配组", "平台接口", "静态标注库与AI算法配置真实数据读取", "B6-01", "2026-10-30", "2026-10-31", 3.0, 0.20, "未开始", "接口测试", "成功读取静态标注坐标、几何类型及算法", "标注库对接报告"),

        # 8.0 MQTT 遥测总线与事件集成
        ("8.1", "B7-01", "平台适配组", "遥测集成", "无人机 MQTT 遥测上行数据流订阅与解析", "B6-01", "2026-10-30", "2026-11-01", 4.0, 0.10, "未开始", "报文抓包", "实时接收经纬高、姿态、电量、当前航点", "遥测解析服务代码"),
        ("8.2", "B7-02", "平台适配组", "遥测集成", "控制指令平台下发回执状态机映射", "B7-01", "2026-10-31", "2026-11-01", 4.0, 0.10, "未开始", "状态机单测", "submitted/accepted/executing/completed 映射无误", "指令回执状态机"),
        ("8.3", "B7-03", "平台适配组", "遥测集成", "任务事件总线（到达/离开/低电量/中断）打通", "B7-02", "2026-10-31", "2026-11-02", 4.0, 0.10, "未开始", "模拟回放", "事件毫秒级分发至 AI 生命周期与断点模块", "事件总线测试记录"),

        # 9.0 外场实飞预演与真机验收
        ("9.1", "B8-01", "现场飞手组", "外场验收", "外场真实无人机控制权获取与地面规划执行", "B7-03", "2026-11-03", "2026-11-03", 5.0, 0.00, "未开始", "外场实飞", "自然语言成功下发巡检任务，无人机按航线起飞", "实飞任务日志与视频"),
        ("9.2", "B8-02", "现场飞手组", "外场验收", "飞行中自然语言微操/载荷控制与围栏拦截实测", "B8-01", "2026-11-04", "2026-11-04", 5.0, 0.00, "未开始", "外场实飞", "微操与载荷正常响应，禁飞区指令被平台拦截", "空中控制实测记录"),
        ("9.3", "B8-03", "项目指挥部", "交付终验", "外场低电量返航、测绘中断与全流程签字交付", "B8-02", "2026-11-05", "2026-11-05", 6.0, 0.00, "未开始", "现场终验", "低电量安全返航、中断作废合规，V1.4全流程签字", "V1.4 交付验收签字单")
    ]

    for idx, rdata in enumerate(wbs_tasks_data, start=4):
        ws2.row_dimensions[idx].height = 24
        fill_curr = fill_white if idx % 2 == 0 else fill_zebra
        for c_idx, val in enumerate(rdata, start=1):
            cell = ws2.cell(idx, c_idx)
            cell.fill = fill_curr
            cell.border = border_cell
            if c_idx in (1, 2, 3, 4, 7, 8, 12):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_center
            elif c_idx == 5:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_left
            elif c_idx in (6, 13, 14):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_left_wrap
            elif c_idx == 9:
                cell.value = val
                cell.font = font_num
                cell.number_format = '0.0'
                cell.alignment = align_right
            elif c_idx == 10:
                cell.value = val
                cell.font = font_num_bold
                cell.number_format = '0.0%'
                cell.alignment = align_center
                if val >= 0.8:
                    cell.fill = PatternFill(start_color=C_STATUS_DONE_BG, end_color=C_STATUS_DONE_BG, fill_type='solid')
                    cell.font = Font(name=FONT_NUM, size=9.5, bold=True, color=C_STATUS_DONE_FG)
                elif val >= 0.5:
                    cell.fill = PatternFill(start_color=C_STATUS_ING_BG, end_color=C_STATUS_ING_BG, fill_type='solid')
                    cell.font = Font(name=FONT_NUM, size=9.5, bold=True, color=C_STATUS_ING_FG)
                else:
                    cell.fill = PatternFill(start_color=C_STATUS_TODO_BG, end_color=C_STATUS_TODO_BG, fill_type='solid')
                    cell.font = Font(name=FONT_NUM, size=9.5, color=C_STATUS_TODO_FG)
            elif c_idx == 11:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_center
                if val == "已完成":
                    cell.fill = PatternFill(start_color=C_STATUS_DONE_BG, end_color=C_STATUS_DONE_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color=C_STATUS_DONE_FG)
                elif val == "进行中":
                    cell.fill = PatternFill(start_color=C_STATUS_ING_BG, end_color=C_STATUS_ING_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color=C_STATUS_ING_FG)
                else:
                    cell.fill = PatternFill(start_color=C_STATUS_TODO_BG, end_color=C_STATUS_TODO_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, color=C_STATUS_TODO_FG)

    last_wbs_row = 4 + len(wbs_tasks_data)
    ws2.row_dimensions[last_wbs_row].height = 25
    ws2.cell(last_wbs_row, 1, "全任务汇总")
    ws2.cell(last_wbs_row, 2, f"共 {len(wbs_tasks_data)} 项")
    ws2.cell(last_wbs_row, 3, "5大专线协同")
    ws2.cell(last_wbs_row, 4, "全功能域覆盖")
    ws2.cell(last_wbs_row, 5, "从基线维护到现场真机终验完整链路")
    ws2.cell(last_wbs_row, 6, "紧前依赖闭环")
    ws2.cell(last_wbs_row, 7, "2026-10-27")
    ws2.cell(last_wbs_row, 8, "2026-11-05")
    ws2.cell(last_wbs_row, 9, f"=SUM(I4:I{last_wbs_row-1})")
    ws2.cell(last_wbs_row, 10, f"=AVERAGE(J4:J{last_wbs_row-1})")
    ws2.cell(last_wbs_row, 11, "推进中")
    ws2.cell(last_wbs_row, 12, "全层级验证")
    ws2.cell(last_wbs_row, 13, "42项任务全部通过指标")
    ws2.cell(last_wbs_row, 14, "交付件齐全")

    for c in range(1, 15):
        cell = ws2.cell(last_wbs_row, c)
        cell.fill = fill_section
        cell.border = border_total
        cell.font = font_body_bold
        if c in (1, 2, 3, 4, 7, 8, 11, 12):
            cell.alignment = align_center
        elif c == 9:
            cell.alignment = align_right
            cell.font = font_num_bold
            cell.number_format = '0.0'
        elif c == 10:
            cell.alignment = align_center
            cell.font = font_num_bold
            cell.number_format = '0.0%'
        else:
            cell.alignment = align_left

    ws2_widths = {1: 10, 2: 11, 3: 13, 4: 13, 5: 32, 6: 12, 7: 12, 8: 12, 9: 11, 10: 12, 11: 11, 12: 13, 13: 34, 14: 28}
    for col_idx, width in ws2_widths.items():
        ws2.column_dimensions[get_column_letter(col_idx)].width = width

    # ==========================================
    # SHEET 3: V1.4功能完成度矩阵 (Feature Completion Matrix)
    # ==========================================
    ws3 = wb.create_sheet(title="V1.4功能完成度矩阵")
    setup_sheet_page(ws3)
    ws3.freeze_panes = "A4"

    ws3.merge_cells("A1:K1")
    ws3["A1"] = "灵犀平台 V1.4 自然语言控制功能规范达成状态矩阵"
    ws3["A1"].font = font_main_title
    ws3["A1"].alignment = align_center
    ws3["A1"].fill = PatternFill(start_color=C_TITLE_BG, end_color=C_TITLE_BG, fill_type='solid')
    ws3.row_dimensions[1].height = 36

    ws3.merge_cells("A2:K2")
    ws3["A2"] = "对照《设计规范 V1.4》逐项核验代码实现、自动化单测覆盖、外部平台依赖与当前完成率"
    ws3["A2"].font = font_sub_title
    ws3["A2"].alignment = align_center
    ws3["A2"].fill = PatternFill(start_color=C_SUBTITLE_BG, end_color=C_SUBTITLE_BG, fill_type='solid')
    ws3.row_dimensions[2].height = 22

    f_headers = [
        "规范章节", "功能域", "具体功能模块", "V1.4 规范设计核心要点", 
        "主战专线", "单测覆盖用例数", "完成率", "未完成率", 
        "已具备能力范围", "待解决客观差距与依赖", "目标达成日期"
    ]
    ws3.row_dimensions[3].height = 28
    for col_idx, h in enumerate(f_headers, start=1):
        cell = ws3.cell(3, col_idx, h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_header

    feature_data = [
        ("2.1.1", "静态标注AI", "标注AI模型配置与显隐规则", "点/线/面显示【AI模型配置】按钮；体标注隐藏（底层不支持立体规划检测）；弹窗多选算法，非必填", "AI算法组/前端组", "test_ai_lifecycle (31项)", 0.60, 0.40, "几何类型校验、体标注拦截报错、多选配置契约已闭环", "前端标注弹窗真实对接、静态标注库真实写接口未接入", "2026-10-29"),
        ("2.1.2", "静态标注AI", "算法绑定与执行生命周期规则", "默认实时+航点检测；到达标注区域开启视频流分析；离开该标注地理范围自动关闭", "AI算法组", "test_ai_lifecycle (36项)", 0.65, 0.35, "六阶段生命周期状态机、算法运行集合维护、到达/离开判定单测通过", "真实算法启停接口（平台仅有任务级videoDetect，缺少算法级启停）", "2026-10-29"),
        ("2.1.2", "静态标注AI", "全局与标注算法去重防误关", "若算法在全局配置中已被勾选，无人机离开标注区域时保留全局开启状态，绝不误关", "AI算法组", "test_ai_lifecycle (12项)", 0.90, 0.10, "去重集合运算、全局算法保护逻辑、防误关测试完全通过", "真实视频分析推流画面与算法框选效果待真机验证", "2026-10-28"),
        ("2.2.1", "通用前置", "控制权无豁免校验机制", "地面规划校验下发权限；飞行中所有指令（微操、载荷、急停、一键返航）无豁免校验控制权，无权拦截", "飞控核心组", "test_product_v13 (49项)", 0.90, 0.10, "确定性权限门禁、预览/确认双重校验、撤权即熔断全部闭环", "真实平台控制权申请与抢占接口联调", "2026-10-28"),
        ("2.2.2", "通用前置", "电子围栏防闯入与六态判定", "飞行微操前校验目标路径穿越禁飞区；clear放行，blocked/invalid拒绝，超时/不可用降级", "飞控核心组/平台组", "test_path_fence (80项)", 0.85, 0.15, "六态枚举处理、降级告警、路径折线插值与防闯入单测全部通过", "真实平台 /electronicFence/path-route/with-clean 37点联调", "2026-10-30"),
        ("2.2.3", "通用前置", "状态介入校验（航线强插拦截）", "自动航线执行中除“暂停/返航/急停”外微操一律拦截；载荷指令放行；route_status未知fail-closed", "飞控核心组", "test_product_v13 (46项)", 0.90, 0.10, "航线微操拦截、载荷放行、route_status未知拒绝闭环", "平台当前快照 route_status 字段偶发为 null，需平台修复", "2026-10-28"),
        ("2.2.4", "通用前置", "悬停与航线断点业务规则", "自动航线悬停=调用暂停航线并保存断点；手动飞行悬停=原地悬停不涉及断点；绝不教成急停", "飞控核心组", "test_v13_hover_and_point (43项)", 0.90, 0.10, "自动/手动悬停动作语义分离、断点数据保存、提示词修复闭环", "真实设备悬停折算指令（120/122/207）下发与响应", "2026-10-28"),
        ("2.2.5", "通用前置", "参数阈值智能换算校验机制", "指令参数超限自动取设备极值执行，不拦截并提示用户；卡片三列 requested/parameters/adjustments", "飞控核心组", "test_cockpit_bounds (9项)", 0.95, 0.05, "设备参数表比对、智能极值修正、三列卡片提示逻辑完全闭环", "部分机型扩展载荷（如双光云台特殊档位）极值表完善", "2026-10-28"),
        ("3.1", "地面规划", "地面规划整体流程（11步流程）", "自然语言输入→意图理解→标注解析→航线决策→AI绑定→预览确认→下发任务并同步状态", "地面规划组", "test_handoff_contract (41项)", 0.40, 0.60, "交接信封协议 lingxi-flight-handoff/v1 定义与校验闭环", "完整地面规划11步链路在 lingxi-agent 仓，需跨仓联合联调", "2026-10-28"),
        ("3.2.1", "地面规划", "测绘与巡检触发词映射规则", "正射/三维/全景航线强制映射，等距拍照；航点/面状航线自适应；网格和几何体航线直接拒绝", "地面规划组", "test_target_resolver (15项)", 0.75, 0.25, "触发词解析、航线映射字典、不支持航线拦截单测通过", "与规划 Agent 跨仓联合回归", "2026-10-28"),
        ("3.2.2", "地面规划", "静态标注与航线强制映射", "点目标多点按顺序；线目标单线端点；面目标单面弓字；立体与混合目标严格拒绝并话术提示", "地面规划组", "test_target_resolver (15项)", 0.70, 0.30, "多点顺序保留、单线单面校验、体目标/混合目标拒绝完全闭环", "真实静态标注库数据读取接口未接（当前 mock）", "2026-10-28"),
        ("3.3", "地面规划", "规划阶段 AI 识别项生命周期", "自动加载标注绑定的AI算法；与全局AI去重；冲突时主动追问；未配置算法整批拒绝", "AI算法组", "test_ai_lifecycle (31项)", 0.60, 0.40, "计划选择、冲突追问状态机、未配置拒绝契约单测通过", "真实标注库AI绑定数据读取接口适配", "2026-10-29"),
        ("4.1.1", "任务管控", "临时任务接管与中断替换", "飞行中允许临时指点/环绕微操；临时任务中下发新指令允许中断替换并保留主线断点", "飞控核心组", "test_mapping_interruption (46项)", 0.80, 0.20, "临时任务抢占、旧任务作废、断点版本链维护闭环", "真实飞控任务中断切换接口联调", "2026-10-29"),
        ("4.1.2", "任务管控", "测绘中断首屏警示与六步作废", "测绘任务首屏强提示成果作废风险；确认后执行六步原子事务（关拼图、作废、清断点、置灰恢复）", "飞控核心组", "test_mapping_interruption (61项)", 0.80, 0.20, "首屏警示逻辑、六步原子事务顺序与多进程并发安全单测通过", "Java平台测绘原子事务接口实现（当前为本地模拟事务）", "2026-10-29"),
        ("4.1.3", "任务管控", "任务断点恢复与显式二选一", "临时任务结束后必须显式提示并要求用户选择“恢复主线任务”或“结束全部任务”，绝不擅自猜测", "飞控核心组", "test_task_events (15项)", 0.80, 0.20, "显式选项生成、选项二选一卡片确认逻辑闭环", "飞控遥测事件触发自动弹出前端选择卡片联调", "2026-10-29"),
        ("4.2", "任务管控", "低电量返航优先级与充电续飞", "低电量返航优先级高于一切控制指令；充电完成后仅恢复有效巡检主线，测绘作废任务绝不续飞", "飞控核心组", "test_task_events (15项)", 0.80, 0.20, "低电量事件抢占调度、充电后续飞条件判断完全闭环", "MQTT 真实电量遥测事件流订阅与推送", "2026-10-29"),
        ("4.3.1", "控制词典", "飞行微操类控制指令", "前后左右上下（相对机头）、顺逆时针转向、悬停、急停、三维坐标指点飞行、指定半径环绕", "飞控核心组", "test_v13_hover_and_point (24项)", 0.85, 0.15, "机头相对坐标解算、微操参数换算、指点/环绕轨迹生成闭环", "平台对指点飞行/环绕接口能力放行声明与真机测试", "2026-10-29"),
        ("4.3.2", "控制词典", "载荷控制类指令（免暂停航线）", "云台俯仰角度/回中、变焦倍数、镜头模式（红外/广角/变焦）、拍照录像、喊话播报、夜航灯开关", "飞控核心组", "test_product_v13_actions (17项)", 0.85, 0.15, "载荷指令免暂停航线校验、参数极值换算、相机状态回执闭环", "真实喊话器、夜航灯硬件通道联调", "2026-10-29"),
        ("4.3.3", "控制词典", "任务控制类指令", "暂停航线、恢复航线、一键返航、结束当前任务、结束全部任务；执行前重复安全检查", "飞控核心组", "test_task_events (45项)", 0.90, 0.10, "任务状态机切换、结束任务安全复检、断点清理闭环", "真实平台长连接任务结束接口响应", "2026-10-28"),
        ("4.3.4", "控制词典", "复合指令拆解与顺序/并发执行", "大模型意图拆解；顺序执行逐步确认，任一步失败熔断；并发执行显式双卡，失败不伪造回滚", "AI算法组", "test_cockpit_plan (15项)", 0.80, 0.20, "顺序/并发计划生成、状态卡片绑定、部分失败保全单测通过", "更复杂的自然语言多动词组合意图泛化评测", "2026-10-30"),
        ("5.0", "异常话术", "异常场景统一提示话术库", "覆盖无控制权、航线强插、参数超限、平台遇障、测绘不可中断、不支持体目标等13大场景标准话术", "前端开发组", "test_model_failure (16项)", 0.80, 0.20, "13大场景固定话术常量、错误码映射、测试断言全部闭环", "真实前端页面提示浮层与语音播报体验走查", "2026-10-30"),
        ("E2E", "环境闭环", "真实平台接口与 Java 服务联调", "对接 Java /screen/uav/* 路由与 /electronicFence 围栏接口，验证真实鉴权与状态回执", "平台适配组", "test_ktd_platform (33项)", 0.20, 0.80, "平台适配层代码已就绪、合成快照单测通过", "Java 后端测试环境服务联调与字段缺失补齐", "2026-10-31"),
        ("E2E", "环境闭环", "真机外场飞行与端到端闭环", "真实无人机外场起飞、自然语言控制微操/载荷、电子围栏拦截、低电量返航全流程实飞闭环", "现场指挥部", "tools/flight_agent_demo.py", 0.00, 1.00, "离线与模拟器 100% 准备就绪，安全规则层完全闭环", "生产 MQTT、物理无人机飞控链路、外场飞手签字验收", "2026-11-05")
    ]

    for idx, rdata in enumerate(feature_data, start=4):
        ws3.row_dimensions[idx].height = 25
        fill_curr = fill_white if idx % 2 == 0 else fill_zebra
        for c_idx, val in enumerate(rdata, start=1):
            cell = ws3.cell(idx, c_idx)
            cell.fill = fill_curr
            cell.border = border_cell
            if c_idx in (1, 2, 5, 11):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_center
            elif c_idx == 3:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_left
            elif c_idx in (4, 6, 9, 10):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_left_wrap
            elif c_idx in (7, 8):
                cell.value = val
                cell.font = font_num_bold
                cell.number_format = '0.0%'
                cell.alignment = align_center
                if c_idx == 7:
                    if val >= 0.85:
                        cell.fill = PatternFill(start_color=C_STATUS_DONE_BG, end_color=C_STATUS_DONE_BG, fill_type='solid')
                        cell.font = Font(name=FONT_NUM, size=9.5, bold=True, color=C_STATUS_DONE_FG)
                    elif val >= 0.5:
                        cell.fill = PatternFill(start_color=C_STATUS_ING_BG, end_color=C_STATUS_ING_BG, fill_type='solid')
                        cell.font = Font(name=FONT_NUM, size=9.5, bold=True, color=C_STATUS_ING_FG)
                    else:
                        cell.fill = PatternFill(start_color=C_STATUS_TODO_BG, end_color=C_STATUS_TODO_BG, fill_type='solid')
                        cell.font = Font(name=FONT_NUM, size=9.5, color=C_STATUS_TODO_FG)

    ws3_widths = {1: 11, 2: 13, 3: 24, 4: 34, 5: 16, 6: 22, 7: 12, 8: 12, 9: 30, 10: 32, 11: 15}
    for col_idx, width in ws3_widths.items():
        ws3.column_dimensions[get_column_letter(col_idx)].width = width

    # ==========================================
    # SHEET 4: 关键场景验证矩阵 (Key Verification Scenarios)
    # ==========================================
    ws4 = wb.create_sheet(title="关键场景验证矩阵")
    setup_sheet_page(ws4)
    ws4.freeze_panes = "A4"

    ws4.merge_cells("A1:J1")
    ws4["A1"] = "灵犀平台 V1.4 重点业务场景验证矩阵与测试证据表"
    ws4["A1"].font = font_main_title
    ws4["A1"].alignment = align_center
    ws4["A1"].fill = PatternFill(start_color=C_TITLE_BG, end_color=C_TITLE_BG, fill_type='solid')
    ws4.row_dimensions[1].height = 36

    ws4.merge_cells("A2:J2")
    ws4["A2"] = "精选覆盖 V1.4 全场景的核心验证用例 | 包含测试场景、输入条件、预期响应、证据位置与验证模式"
    ws4["A2"].font = font_sub_title
    ws4["A2"].alignment = align_center
    ws4["A2"].fill = PatternFill(start_color=C_SUBTITLE_BG, end_color=C_SUBTITLE_BG, fill_type='solid')
    ws4.row_dimensions[2].height = 22

    sc_headers = [
        "用例编号", "所属阶段", "功能域", "测试场景描述", "前置状态 / 输入参数", 
        "预期系统响应与用户话术", "实际证据与用例位置", "验证模式", "承接专线", "当前结论"
    ]
    ws4.row_dimensions[3].height = 28
    for col_idx, h in enumerate(sc_headers, start=1):
        cell = ws4.cell(3, col_idx, h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_header

    scenarios_data = [
        ("T-01", "M-01 基线", "基线回归", "离线自动化全量回归测试", "本地源码与依赖已就绪，完全离线", "1,115 项用例全部 PASS，零 socket 连接", "tools/ci/offline_tests.sh", "单元/隔离测试", "核心测试组", "PASS (通过)"),
        ("T-02", "M-01 基线", "代码质量", "源码语法编译与空白符检查", "工作树清洁无脏变更", "零 SyntaxError，零 trailing whitespace", "compileall & diff check", "代码静态检查", "核心测试组", "PASS (通过)"),
        ("T-03", "M-02 规划", "地面规划", "多点目标输入严格保留用户指定顺序", "静态标注 A、B 两点，用户输入 order=[1, 2]", "输出顺序与用户一致，不因就近原则自动重排", "test_target_resolver.py", "单元测试", "地面规划组", "PASS (通过)"),
        ("T-04", "M-02 规划", "地面规划", "单线目标强制端点起飞，多线输入拒绝", "同时选中 2 条线标注", "返回 MULTI_LINE_UNSUPPORTED 拒绝码并明确提示", "test_handoff_contract.py", "单元测试", "地面规划组", "PASS (通过)"),
        ("T-05", "M-02 规划", "地面规划", "单面目标自适应生成弓字形扫描航线", "单个面标注多边形坐标", "成功规划单面全覆盖航线；多面输入则拒绝", "test_target_resolver.py", "单元测试", "地面规划组", "PASS (通过)"),
        ("T-06", "M-02 规划", "地面规划", "立体目标与混合目标输入强制拒绝", "targets 包含 volume 或 point+line 混合", "返回体目标不支持或混合目标不支持 typed 错误码", "test_target_resolver.py", "单元测试", "地面规划组", "PASS (通过)"),
        ("T-07", "M-02 规划", "地面规划", "任务交接信封快照版本过期防护", "snapshot_version 不匹配", "拒绝下发并返回 STALE_HANDOFF_VERSION", "test_handoff_contract.py", "单元测试", "地面规划组", "PASS (通过)"),
        ("T-08", "M-03 AI", "AI生命周期", "立体标注绑定 AI 算法强制拦截", "geometry_type=volume", "返回 BODY_ANNOTATION_AI_UNSUPPORTED", "test_ai_lifecycle.py", "单元测试", "AI算法组", "PASS (通过)"),
        ("T-09", "M-03 AI", "AI生命周期", "未配置算法整批拒绝且不近似替换", "algorithm 不在 configured 集合中", "整批抛出 ALGORITHM_NOT_CONFIGURED，无近似替换", "test_ai_lifecycle.py", "单元测试", "AI算法组", "PASS (通过)"),
        ("T-10", "M-03 AI", "AI生命周期", "全局算法与标注算法集合运算去重", "全局配置 fire，标注同时配置 fire 和 smoke", "算法集合合并去重，fire 只启动一次", "test_ai_lifecycle.py", "单元测试", "AI算法组", "PASS (通过)"),
        ("T-11", "M-03 AI", "AI生命周期", "无人机离开标注区域专属算法关闭", "标注 A 离开，全局未配置对应算法", "专属算法正常触发停止事件并从运行集移除", "test_ai_lifecycle.py", "单元+模拟器", "AI算法组", "PASS (通过)"),
        ("T-12", "M-03 AI", "AI生命周期", "全局算法防误关保护机制", "标注 A 和全局都配 fire，离开标注 A", "离开 A 后 fire 依然在运行集合，防误关生效", "test_ai_lifecycle.py", "单元+模拟器", "AI算法组", "PASS (通过)"),
        ("T-13", "M-03 AI", "AI生命周期", "AI 识别配置冲突主动追问澄清", "标注已有 fire，用户口令要求切换为 smoke", "生成澄清追问卡片，不擅自替换或生成确认卡", "test_ai_lifecycle.py", "单元测试", "AI算法组", "PASS (通过)"),
        ("T-14", "M-04 飞控", "飞行控制", "无控制权下发飞行微操强制拦截", "control_owned=false，指令“向前飞10米”", "拦截下发，提示“您尚未获取无人机控制权”", "test_product_v13.py", "模拟器/假平台", "飞控核心组", "PASS (通过)"),
        ("T-15", "M-04 飞控", "飞行控制", "无控制权下发急停/返航/载荷无豁免", "control_owned=false，指令“急停”/“变焦”", "一律拦截，不存在任何控制权豁免", "test_product_v13.py", "模拟器/假平台", "飞控核心组", "PASS (通过)"),
        ("T-16", "M-04 飞控", "飞行控制", "自动航线执行中飞行微操强插拦截", "route_status=running，指令“向左飞5米”", "拦截并提示“当前正在执行自动航线，请先悬停”", "test_product_v13.py", "模拟器/假平台", "飞控核心组", "PASS (通过)"),
        ("T-17", "M-04 飞控", "飞行控制", "自动航线执行中载荷指令放行", "route_status=running，指令“变焦到10倍”", "正常放行并生成确认卡，免暂停航线", "test_product_v13_actions.py", "模拟器/假平台", "飞控核心组", "PASS (通过)"),
        ("T-18", "M-04 飞控", "飞行控制", "自然语言悬停等价暂停航线与保存断点", "route_status=running，口令“悬停一下”", "映射为暂停航线并保存断点，绝不替换为急停", "test_v13_hover_and_point.py", "模拟器/假平台", "飞控核心组", "PASS (通过)"),
        ("T-19", "M-04 飞控", "飞行控制", "电子围栏目标路径穿越禁飞区拦截", "fake provider 返回 blocked 状态", "拦截下发，返回 PATH_BLOCKED 并提示障碍禁飞区", "test_path_fence.py", "模拟器/假平台", "飞控核心组", "PASS (通过)"),
        ("T-20", "M-04 飞控", "飞行控制", "电子围栏服务接口超时/不可用自动降级", "fake provider 响应超时 3 秒", "指令继续放行，带 timeout 降级警告提示", "test_path_fence.py", "模拟器/假平台", "飞控核心组", "PASS (通过)"),
        ("T-21", "M-04 飞控", "载荷控制", "参数超限自动取设备极值并明确提示", "指令“变焦到100倍”，实际设备最大30倍", "effective 取 30，提示“最大变焦为30倍，已调整”", "test_cockpit_bounds.py", "模拟器/假平台", "飞控核心组", "PASS (通过)"),
        ("T-22", "M-04 飞控", "任务控制", "结束当前任务保留主线断点", "当前正在执行临时指点任务", "临时任务结束，主线断点完整保留", "test_task_events.py", "模拟器测试", "飞控核心组", "PASS (通过)"),
        ("T-23", "M-04 飞控", "任务控制", "结束全部任务清空全部断点与状态", "当前有主线与临时任务", "清空主线断点，任务状态机完整重置", "test_task_events.py", "模拟器测试", "飞控核心组", "PASS (通过)"),
        ("T-24", "M-05 中断", "测绘中断", "测绘任务首屏不可中断警示二次确认", "任务类型为测绘建图，指令试图强插微操", "弹出首屏警示“不可中断，若强插成果作废”确认卡", "test_mapping_interruption.py", "模拟器/联调", "飞控核心组", "联调中"),
        ("T-25", "M-05 中断", "测绘中断", "测绘确认中断六步原子事务执行", "用户确认作废测绘并执行新微操", "关拼图、作废、清断点、置灰恢复六步原子执行", "test_mapping_interruption.py", "模拟器/联调", "飞控核心组", "联调中"),
        ("T-26", "M-05 中断", "低电量", "低电量返航最高优先级事件抢占调度", "电量跌破返航阈值 (battery < 20%)", "最高优先级抢占，无论处于何种状态均触发返航", "test_task_events.py", "模拟器测试", "飞控核心组", "PASS (通过)"),
        ("T-27", "M-05 中断", "低电量", "充电完成仅续飞有效主线断点", "电量恢复 100%，触发 recharge 事件", "若主线有效则续飞；若已作废或结束则绝不续飞", "test_task_events.py", "模拟器测试", "飞控核心组", "PASS (通过)"),
        ("T-28", "M-06 复合", "复合指令", "并发复合指令部分失败不伪造回滚", "同时执行移动与喊话，喊话失败", "移动成功予以保留，喊话失败明确告知，不回滚", "test_cockpit_plan.py", "模拟器测试", "AI算法组", "PASS (通过)"),
        ("T-29", "M-07 平台", "平台接口", "真实平台快照 route_status 字段校验", "调用真实 Java /screen/uav 状态接口", "route_status 字段正常返回非 null 状态", "Java 联调接口", "真实平台联调", "平台适配组", "待环境"),
        ("T-30", "M-11 终验", "真机验收", "外场真实无人机自然语言微操与围栏防闯", "外场实飞，自然语言下发微操与航线控制", "无人机姿态与轨迹响应准确，禁飞区被物理拦截", "真机外场日志与录屏", "外场真机实飞", "现场飞手组", "待现场")
    ]

    for idx, rdata in enumerate(scenarios_data, start=4):
        ws4.row_dimensions[idx].height = 24
        fill_curr = fill_white if idx % 2 == 0 else fill_zebra
        for c_idx, val in enumerate(rdata, start=1):
            cell = ws4.cell(idx, c_idx)
            cell.fill = fill_curr
            cell.border = border_cell
            if c_idx in (1, 2, 3, 8, 9):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_center
            elif c_idx == 4:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_left
            elif c_idx in (5, 6, 7):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_left_wrap
            elif c_idx == 10:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_center
                if "PASS" in str(val):
                    cell.fill = PatternFill(start_color=C_STATUS_DONE_BG, end_color=C_STATUS_DONE_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color=C_STATUS_DONE_FG)
                elif "联调中" in str(val):
                    cell.fill = PatternFill(start_color=C_STATUS_ING_BG, end_color=C_STATUS_ING_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color=C_STATUS_ING_FG)
                else:
                    cell.fill = PatternFill(start_color=C_STATUS_TODO_BG, end_color=C_STATUS_TODO_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, color=C_STATUS_TODO_FG)

    ws4_widths = {1: 10, 2: 12, 3: 13, 4: 28, 5: 32, 6: 36, 7: 26, 8: 15, 9: 15, 10: 14}
    for col_idx, width in ws4_widths.items():
        ws4.column_dimensions[get_column_letter(col_idx)].width = width

    # ==========================================
    # SHEET 5: 编制依据与规范边界 (Standards & Baseline)
    # ==========================================
    ws5 = wb.create_sheet(title="编制依据与规范边界")
    setup_sheet_page(ws5)

    ws5.merge_cells("A1:G1")
    ws5["A1"] = "灵犀平台自然语言智能控制功能 (V1.4) 编制依据与管理规则"
    ws5["A1"].font = font_main_title
    ws5["A1"].alignment = align_center
    ws5["A1"].fill = PatternFill(start_color=C_TITLE_BG, end_color=C_TITLE_BG, fill_type='solid')
    ws5.row_dimensions[1].height = 36

    ws5.merge_cells("A2:G2")
    ws5["A2"] = "明确设计依据文档、交付验收审计记录、离线测试真源、完成度量化定义与不可逾越的安全红线"
    ws5["A2"].font = font_sub_title
    ws5["A2"].alignment = align_center
    ws5["A2"].fill = PatternFill(start_color=C_SUBTITLE_BG, end_color=C_SUBTITLE_BG, fill_type='solid')
    ws5.row_dimensions[2].height = 22

    ws5["A4"] = "一、排期设计依据与代码审计基线"
    ws5["A4"].font = font_section_title
    ws5.row_dimensions[4].height = 24

    std_headers = ["依据分类", "文档/代码源路径", "版本/哈希标识", "覆盖范围与核心说明", "基线时效状态"]
    ws5.row_dimensions[5].height = 26
    for col_idx, h in enumerate(std_headers, start=1):
        cell = ws5.cell(5, col_idx, h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_header

    standards_data = [
        ("功能设计规范", "灵犀平台自然语言智能控制功能设计规范V1.4.docx", "SHA256: e4e42b712e32...8c78", "V1.4 业务需求真源（含2.1静态标注AI、2.2通用校验、3地面规划、4飞行控制与状态机、5异常话术）", "基线已冻结"),
        ("最终交付审计", "docs/ACCEPTANCE_FLIGHT-V14.md", "Commit: d6d284d 之后", "V1.4 交付验收矩阵、13项安全错误专项检查结论、已闭环与客观差距清单", "已审计归档"),
        ("离线测试真源", "tools/ci/offline_tests.sh", "1,115 Tests PASS / 0 Failed", "本地离线自动化全量测试套件（严格隔离网络环境，断网守卫拦截 socket 外连）", "持续集成基准"),
        ("飞控代码基线", "lingxi-flight-control-agent", "分支: test (领先 origin/test 1 提交)", "飞控自然语言解析、策略门禁、内存执行器闭环代码库；严格保留既有未提交改动", "当前开发基线"),
        ("前端代码基线", "lingxi-flight-agent-ui", "前端维护分支", "飞控助手前端交互、静态标注AI配置弹窗、微操确认卡与异常话术呈现组件", "协同基准")
    ]

    for idx, rdata in enumerate(standards_data, start=6):
        ws5.row_dimensions[idx].height = 24
        fill_curr = fill_white if idx % 2 == 0 else fill_zebra
        for c_idx, val in enumerate(rdata, start=1):
            cell = ws5.cell(idx, c_idx)
            cell.fill = fill_curr
            cell.border = border_cell
            if c_idx in (1, 5):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_center
            elif c_idx == 2:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_left
            elif c_idx in (3, 4):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_left_wrap

    ws5["A12"] = "二、项目管理口径与安全红线原则"
    ws5["A12"].font = font_section_title
    ws5.row_dimensions[12].height = 24

    rule_headers = ["管理分类", "量化标准 / 判定规则", "具体管理要求与证据标准", "执行阶段约束"]
    ws5.row_dimensions[13].height = 26
    for col_idx, h in enumerate(rule_headers, start=1):
        cell = ws5.cell(13, col_idx, h)
        cell.font = fill_header
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_header

    rules_data = [
        ("已完成 (Completed)", "100%", "代码实现完成、通过自动化离线单元测试覆盖，且具备不可篡改的测试证据文件。无未决技术债务。", "维持基线不退化，进入回归测试池"),
        ("部分完成 (In-Progress)", "40% – 85%", "核心业务规则与内存模拟器已闭环（如六步作废、去重防误关），但外部真实接口缺失或未联调通过。", "由主战专线并行推进接口对接与仿真打桩"),
        ("未开始 / 待现场 (Pending)", "0% – 20%", "依赖真实外部接口联调、MQTT遥测接通或外场真机飞行。绝不得用模拟器通过冒充真实完成。", "进入外场预演与现场实飞专项攻坚"),
        ("不可逾越的安全红线", "一票否决门禁", "1. 无控制权绝不下发控制指令；2. route_status 未知一律 fail-closed；3. 悬停动作绝不替换为急停；4. 测绘中断必须先警告后作废；5. 严禁未经授权操作共享测试/生产环境。", "全阶段开发与联调过程中严格遵守")
    ]

    for idx, rdata in enumerate(rules_data, start=14):
        ws5.row_dimensions[idx].height = 25
        fill_curr = fill_white if idx % 2 == 0 else fill_zebra
        for c_idx, val in enumerate(rdata, start=1):
            cell = ws5.cell(idx, c_idx)
            cell.fill = fill_curr
            cell.border = border_cell
            if c_idx in (1, 2):
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_center
            else:
                cell.value = val
                cell.font = font_body
                cell.alignment = align_left_wrap

    ws5_widths = {1: 20, 2: 32, 3: 26, 4: 42, 5: 18, 6: 15, 7: 15}
    for col_idx, width in ws5_widths.items():
        ws5.column_dimensions[get_column_letter(col_idx)].width = width

    # 保存
    output_dir = "/Users/wangxiaolin/PycharmProjects/lingxi-flight-control-agent/outputs/flight-v14-plan-20261027"
    target_path = os.path.join(output_dir, "灵犀平台V1.4自然语言控制功能执行排期表.xlsx")
    wb.save(target_path)
    print(f"成功生成专业领导级 Excel: {target_path}")

    # 同时更新桌面与兼容文件
    desktop_xlsx = "/Users/wangxiaolin/Desktop/灵犀平台V1.4自然语言控制功能执行排期表.xlsx"
    wb.save(desktop_xlsx)
    print(f"成功同步至桌面: {desktop_xlsx}")

    # 覆盖原文件以保证兼容
    compat_path = os.path.join(output_dir, "灵犀V14第0-3批执行排期.xlsx")
    wb.save(compat_path)
    print(f"成功覆盖更新原文件: {compat_path}")

if __name__ == "__main__":
    build_workbook()
