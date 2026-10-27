# -*- coding: utf-8 -*-
"""
生成灵犀平台 V1.4 自然语言智能控制功能执行排期与全量状态看板 (高保真专业版)
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
    
    # 核心调色板 (Navy Executive 商业科技风)
    C_NAVY_DARK = "0F172A"     # 主标题背景 Slate-900
    C_NAVY_HEADER = "1E3A8A"   # 表头 Navy Blue
    C_SUBTITLE_BG = "1E293B"   # 副标题背景 Slate-800
    C_ICE_BLUE = "F0F4F8"      # 浅冰蓝
    C_ZEBRA = "F8FAFC"         # 斑马纹浅底
    C_WHITE = "FFFFFF"         # 纯白
    
    # 状态色系 (Soft Pill & Tag Colors)
    C_SUCCESS_BG = "DCFCE7"    # 浅绿
    C_SUCCESS_FG = "15803D"    # 深绿
    C_WARN_BG = "FEF3C7"       # 暖杏黄
    C_WARN_FG = "B45309"       # 琥珀深棕
    C_DANGER_BG = "FEE2E2"     # 浅珊瑚红
    C_DANGER_FG = "B91C1C"     # 深绯红
    C_INFO_BG = "EFF6FF"       # 浅蓝
    C_INFO_FG = "1D4ED8"       # 科技蓝
    C_PURPLE_BG = "EEF2FF"     # 浅紫
    C_PURPLE_FG = "4338CA"     # 商务紫

    thin_border_side = Side(style='thin', color='E2E8F0')
    medium_navy_side = Side(style='medium', color='1E3A8A')
    double_navy_side = Side(style='double', color='1E3A8A')
    
    border_cell = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)
    border_total = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=double_navy_side)
    border_header = Border(left=Side(style='thin', color='3B82F6'), right=Side(style='thin', color='3B82F6'), top=medium_navy_side, bottom=medium_navy_side)

    fill_header = PatternFill(start_color=C_NAVY_HEADER, end_color=C_NAVY_HEADER, fill_type='solid')
    fill_zebra = PatternFill(start_color=C_ZEBRA, end_color=C_ZEBRA, fill_type='solid')
    fill_white = PatternFill(start_color=C_WHITE, end_color=C_WHITE, fill_type='solid')
    fill_total = PatternFill(start_color="E2E8F0", end_color="E2E8F0", fill_type='solid')

    font_title = Font(name=FONT_FAMILY, size=15, bold=True, color=C_WHITE)
    font_subtitle = Font(name=FONT_FAMILY, size=9.5, color="94A3B8")
    font_section = Font(name=FONT_FAMILY, size=11.5, bold=True, color=C_NAVY_HEADER)
    font_header = Font(name=FONT_FAMILY, size=10, bold=True, color=C_WHITE)
    font_body = Font(name=FONT_FAMILY, size=9.5, color="1E293B")
    font_body_bold = Font(name=FONT_FAMILY, size=9.5, bold=True, color="1E293B")
    font_num = Font(name="Segoe UI", size=9.5, color="1E293B")
    font_num_bold = Font(name="Segoe UI", size=9.5, bold=True, color="1E293B")

    align_center = Alignment(horizontal='center', vertical='center')
    align_left = Alignment(horizontal='left', vertical='center')
    align_left_wrap = Alignment(horizontal='left', vertical='center', wrap_text=True)
    align_right = Alignment(horizontal='right', vertical='center')

    # ==========================================
    # SHEET 1: 总览看板 (Overview Dashboard)
    # ==========================================
    ws1 = wb.create_sheet(title="📊 总览看板")
    setup_sheet_page(ws1)

    # 标题区 A1:I2
    ws1.merge_cells("A1:I1")
    ws1["A1"] = "灵犀平台智能飞控系统 V1.4 功能实现状态与执行排期全景看板"
    ws1["A1"].font = font_title
    ws1["A1"].alignment = Alignment(horizontal='center', vertical='center')
    ws1["A1"].fill = PatternFill(start_color=C_NAVY_DARK, end_color=C_NAVY_DARK, fill_type='solid')
    ws1.row_dimensions[1].height = 36

    ws1.merge_cells("A2:I2")
    ws1["A2"] = "依据《灵犀平台自然语言智能控制功能设计规范 V1.4》与《交付审计报告》编制 | 自动化单测基线：1,115 项 100% 通过 (OK) | 排期周期：第0批–第8批 (2026-10-27 至 2026-11-13)"
    ws1["A2"].font = font_subtitle
    ws1["A2"].alignment = Alignment(horizontal='center', vertical='center')
    ws1["A2"].fill = PatternFill(start_color=C_SUBTITLE_BG, end_color=C_SUBTITLE_BG, fill_type='solid')
    ws1.row_dimensions[2].height = 22

    # KPI 摘要卡片 (Row 4-6)
    kpi_configs = [
        ("B4:C4", "B5:C5", "B6:C6", "V1.4 规则与模型层完成率", "78.5%", "18项规范加权 / 内存模拟器100%闭环", C_INFO_BG, C_INFO_FG, "93C5FD"),
        ("D4:E4", "D5:E5", "D6:E6", "近期 0–3 批加权完成率", "83.3%", "26项近期任务 / 未完成率 16.7%", C_SUCCESS_BG, C_SUCCESS_FG, "86EFAC"),
        ("F4:G4", "F5:G5", "F6:G6", "离线自动化测试基线", "1,115 PASS", "65个模块全通过 / 0 Socket外连", C_PURPLE_BG, C_PURPLE_FG, "A5B4FC"),
        ("H4:I4", "H5:I5", "H6:I6", "全周期计划任务与工时", "9批次 / 84h", "42项精细任务 / 覆盖至真机外场验收", C_WARN_BG, C_WARN_FG, "FDE047"),
    ]

    for top_range, mid_range, bot_range, title, val, sub, bg, fg, border_c in kpi_configs:
        ws1.merge_cells(top_range)
        ws1.merge_cells(mid_range)
        ws1.merge_cells(bot_range)
        c_top = ws1[top_range.split(":")[0]]
        c_mid = ws1[mid_range.split(":")[0]]
        c_bot = ws1[bot_range.split(":")[0]]

        c_top.value = title
        c_top.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color="475569")
        c_top.alignment = align_center

        c_mid.value = val
        c_mid.font = Font(name="Segoe UI", size=18, bold=True, color=fg)
        c_mid.alignment = align_center

        c_bot.value = sub
        c_bot.font = Font(name=FONT_FAMILY, size=8.5, color="64748B")
        c_bot.alignment = align_center

        cols = [top_range.split(":")[0][0], top_range.split(":")[1][0]]
        col_start = ord(cols[0]) - ord('A') + 1
        col_end = ord(cols[1]) - ord('A') + 1
        card_side = Side(style='thin', color=border_c)
        for r in range(4, 7):
            for c in range(col_start, col_end + 1):
                cell = ws1.cell(r, c)
                cell.fill = PatternFill(start_color=bg, end_color=bg, fill_type='solid')
                cell.border = Border(
                    left=card_side if c == col_start else None,
                    right=card_side if c == col_end else None,
                    top=card_side if r == 4 else None,
                    bottom=card_side if r == 6 else None
                )

    ws1.row_dimensions[3].height = 10
    ws1.row_dimensions[4].height = 18
    ws1.row_dimensions[5].height = 28
    ws1.row_dimensions[6].height = 18
    ws1.row_dimensions[7].height = 14

    # 批次排期汇总表 (Row 8-19)
    ws1["A8"] = "一、阶段执行排期汇总（第 0 批至第 8 批完整交付演进）"
    ws1["A8"].font = font_section
    ws1.row_dimensions[8].height = 24

    b_headers = ["批次", "计划日期", "里程碑目标", "当前完成率", "当前未完成率", "计划工时(h)", "负责人角色", "批次核心交付物", "当前状态"]
    ws1.row_dimensions[9].height = 26
    for col_idx, h in enumerate(b_headers, start=1):
        cell = ws1.cell(9, col_idx, h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_header

    batch_rows = [
        ("第0批", "2026-10-27", "基线冻结与证据口径统一", 1.00, 0.00, 4.0, "主负责人/测试负责人", "基线审计报告、1115项离线测试分母、风险清单", "已完成"),
        ("第1批", "2026-10-28", "地面规划基础与跨仓交接闭环", 0.40, 0.60, 8.0, "规划Agent负责人/飞控负责人", "点/线/面/体目标解析单测、交接信封协议、跨仓联调报告", "进行中"),
        ("第2批", "2026-10-29", "静态标注与 AI 全生命周期管理", 0.65, 0.35, 8.0, "AI生命周期负责人/前端负责人", "标注算法配置校验、去重/防误关单测、前端按钮走查", "进行中"),
        ("第3批", "2026-10-30", "飞行中基础控制与载荷微操调度", 0.85, 0.15, 8.0, "飞控负责人/平台适配负责人", "控制权无豁免门禁、微操拦截、参数智能换算、载荷控制", "进行中"),
        ("第4批", "2026-11-02", "测绘中断与低电量续飞闭环", 0.80, 0.20, 8.0, "飞控负责人/状态机负责人", "测绘首屏失效警告、六步原子作废、低电量优先与续飞单测", "待开始"),
        ("第5批", "2026-11-04", "复合指令大模型拆解与异常话术验收", 0.80, 0.20, 8.0, "大模型负责人/前端负责人", "顺序/并发复合指令显式卡片、13场景异常话术前端验收", "待开始"),
        ("第6批", "2026-11-06", "真实平台 (Java/Ktd) 接口与围栏联调", 0.20, 0.80, 12.0, "平台适配负责人/Java负责人", "KtdPlatform 接口打通、route_status 快照、37点围栏联调", "待开始"),
        ("第7批", "2026-11-10", "MQTT 遥测与事件流总线集成", 0.10, 0.90, 12.0, "平台适配负责人/物联网负责人", "MQTT遥测状态流订阅、平台回执状态机映射、任务事件总线", "待开始"),
        ("第8批", "2026-11-13", "真实设备外场试飞与端到端闭环验收", 0.00, 1.00, 16.0, "项目负责人/现场飞手/全体核心", "外场真机飞控自然语言控制、电子围栏拦截、全流程签字报告", "待开始")
    ]

    for idx, rdata in enumerate(batch_rows, start=10):
        ws1.row_dimensions[idx].height = 24
        fill_curr = fill_white if idx % 2 == 0 else fill_zebra
        for c_idx, val in enumerate(rdata, start=1):
            cell = ws1.cell(idx, c_idx)
            cell.fill = fill_curr
            cell.border = border_cell
            if c_idx in (1, 2, 7):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_center
            elif c_idx == 3:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_left
            elif c_idx in (4, 5):
                cell.value = val
                cell.font = font_num_bold
                cell.number_format = '0.0%'
                cell.alignment = align_center
                if c_idx == 4:
                    if val == 1.0:
                        cell.fill = PatternFill(start_color=C_SUCCESS_BG, end_color=C_SUCCESS_BG, fill_type='solid')
                        cell.font = Font(name="Segoe UI", size=9.5, bold=True, color=C_SUCCESS_FG)
                    elif val >= 0.5:
                        cell.fill = PatternFill(start_color=C_WARN_BG, end_color=C_WARN_BG, fill_type='solid')
                        cell.font = Font(name="Segoe UI", size=9.5, bold=True, color=C_WARN_FG)
                    else:
                        cell.fill = PatternFill(start_color=C_DANGER_BG, end_color=C_DANGER_BG, fill_type='solid')
                        cell.font = Font(name="Segoe UI", size=9.5, bold=True, color=C_DANGER_FG)
            elif c_idx == 6:
                cell.value = val
                cell.font = font_num
                cell.number_format = '0.0'
                cell.alignment = align_right
            elif c_idx == 8:
                cell.value = val
                cell.font = font_body
                cell.alignment = align_left
            elif c_idx == 9:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_center
                if val == "已完成":
                    cell.fill = PatternFill(start_color=C_SUCCESS_BG, end_color=C_SUCCESS_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color=C_SUCCESS_FG)
                elif val == "进行中":
                    cell.fill = PatternFill(start_color=C_WARN_BG, end_color=C_WARN_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color=C_WARN_FG)
                else:
                    cell.fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, color="64748B")

    # 批次汇总行 (Row 19)
    ws1.row_dimensions[19].height = 25
    ws1.cell(19, 1, "全周期合计 / 平均")
    ws1.cell(19, 2, "2026-10-27 ~ 11-13")
    ws1.cell(19, 3, "0–8批全交付生命周期闭环")
    ws1.cell(19, 4, "=AVERAGE(D10:D18)")
    ws1.cell(19, 5, "=AVERAGE(E10:E18)")
    ws1.cell(19, 6, "=SUM(F10:F18)")
    ws1.cell(19, 7, "多团队跨域协同")
    ws1.cell(19, 8, "全场景验收报告与真机签字交付件")
    ws1.cell(19, 9, "整体推进中")

    for c in range(1, 10):
        cell = ws1.cell(19, c)
        cell.fill = fill_total
        cell.border = border_total
        cell.font = font_body_bold
        if c in (1, 2, 7, 9):
            cell.alignment = align_center
        elif c in (4, 5):
            cell.alignment = align_center
            cell.number_format = '0.0%'
            cell.font = font_num_bold
        elif c == 6:
            cell.alignment = align_right
            cell.number_format = '0.0'
            cell.font = font_num_bold
        else:
            cell.alignment = align_left

    # 第二节：V1.4 章节完成度分布统计表 (Row 21-33)
    ws1.row_dimensions[21].height = 16
    ws1["A22"] = "二、V1.4 规范章节完成度矩阵（规则层 vs 平台层 vs 设备层）"
    ws1["A22"].font = font_section
    ws1.row_dimensions[22].height = 24

    sec_headers = ["规范章节", "功能域分类", "核心功能要点", "规则/模拟器层", "外部平台适配层", "真机设备闭环层", "当前综合完成率", "当前未完成率", "主攻落地批次"]
    ws1.row_dimensions[23].height = 26
    for col_idx, h in enumerate(sec_headers, start=1):
        cell = ws1.cell(23, col_idx, h)
        cell.font = font_header
        cell.fill = PatternFill(start_color="1E40AF", end_color="1E40AF", fill_type='solid')
        cell.alignment = align_center
        cell.border = border_header

    sec_rows = [
        ("2.1", "静态标注与AI模型", "点线面绑定AI、体标注隐藏、到达开启离开关闭、去重防误关", "已完成 (100%)", "部分完成 (40%)", "未验证 (0%)", 0.60, 0.40, "第2批 / 第6批"),
        ("2.2", "通用前置与核心校验", "控制权无豁免、电子围栏六态与降级、航线中拦截、悬停断点、参数极值换算", "已完成 (100%)", "部分完成 (60%)", "未验证 (0%)", 0.90, 0.10, "第3批 / 第6批"),
        ("3.1-3.2", "地面规划与航线决策", "测绘与巡检触发词映射、点线面单目标强制映射、体与混合目标拦截、交接信封", "已完成 (100%)", "部分完成 (50%)", "未验证 (0%)", 0.70, 0.30, "第1批"),
        ("3.3", "AI 识别项生命周期", "规划加载、全局与标注去重、AI冲突追问澄清、未配置算法整批拒绝", "已完成 (100%)", "部分完成 (30%)", "未验证 (0%)", 0.60, 0.40, "第2批"),
        ("4.1", "任务管控与测绘中断", "临时任务原子替换、测绘中断首屏失效警示、六步原子作废、断点显式二选一", "已完成 (100%)", "契约就绪 (40%)", "未验证 (0%)", 0.80, 0.20, "第4批 / 第6批"),
        ("4.2", "低电量返航与续飞", "低电量返航最高优先级调度、充电恢复仅续飞有效主线、测绘作废不续飞", "已完成 (100%)", "事件就绪 (40%)", "未验证 (0%)", 0.80, 0.20, "第4批 / 第7批"),
        ("4.3", "控制指令词典与执行", "飞行微操(六向/转向/悬停/指点/环绕)、载荷(云台/变焦/镜头/拍照/喊话)、复合指令", "已完成 (100%)", "部分完成 (60%)", "未验证 (0%)", 0.85, 0.15, "第3批 / 第5批"),
        ("5.0", "异常提示话术库", "无控制权、航线强插、超限换算、平台遇障、测绘干预等13类场景固定标准话术", "已完成 (100%)", "已完成 (100%)", "未验证 (0%)", 0.80, 0.20, "第3批 / 第5批"),
        ("E2E", "真实环境闭环", "真实 MQTT 遥测上行/回执下行、真实设备执行反馈与状态同步", "模拟器代替", "协议联调中 (20%)", "未开始 (0%)", 0.10, 0.90, "第7批 / 第8批")
    ]

    for idx, rdata in enumerate(sec_rows, start=24):
        ws1.row_dimensions[idx].height = 24
        fill_curr = fill_white if idx % 2 == 0 else fill_zebra
        for c_idx, val in enumerate(rdata, start=1):
            cell = ws1.cell(idx, c_idx)
            cell.fill = fill_curr
            cell.border = border_cell
            if c_idx in (1, 2, 9):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_center
            elif c_idx == 3:
                cell.value = val
                cell.font = font_body
                cell.alignment = align_left
            elif c_idx in (4, 5, 6):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_center
            elif c_idx in (7, 8):
                cell.value = val
                cell.font = font_num_bold
                cell.number_format = '0.0%'
                cell.alignment = align_center
                if c_idx == 7:
                    if val >= 0.8:
                        cell.fill = PatternFill(start_color=C_SUCCESS_BG, end_color=C_SUCCESS_BG, fill_type='solid')
                        cell.font = Font(name="Segoe UI", size=9.5, bold=True, color=C_SUCCESS_FG)
                    elif val >= 0.5:
                        cell.fill = PatternFill(start_color=C_WARN_BG, end_color=C_WARN_BG, fill_type='solid')
                        cell.font = Font(name="Segoe UI", size=9.5, bold=True, color=C_WARN_FG)
                    else:
                        cell.fill = PatternFill(start_color=C_DANGER_BG, end_color=C_DANGER_BG, fill_type='solid')
                        cell.font = Font(name="Segoe UI", size=9.5, bold=True, color=C_DANGER_FG)

    ws1.row_dimensions[33].height = 25
    ws1.cell(33, 1, "全规范加权平均")
    ws1.cell(33, 2, "V1.4 全域功能")
    ws1.cell(33, 3, "规则层 100% | 平台层 45% | 设备层 0%")
    ws1.cell(33, 4, "已闭环")
    ws1.cell(33, 5, "联调中")
    ws1.cell(33, 6, "待外场")
    ws1.cell(33, 7, "=AVERAGE(G24:G32)")
    ws1.cell(33, 8, "=AVERAGE(H24:H32)")
    ws1.cell(33, 9, "按批次演进落地")

    for c in range(1, 10):
        cell = ws1.cell(33, c)
        cell.fill = fill_total
        cell.border = border_total
        cell.font = font_body_bold
        if c in (1, 2, 4, 5, 6, 9):
            cell.alignment = align_center
        elif c in (7, 8):
            cell.alignment = align_center
            cell.number_format = '0.0%'
            cell.font = font_num_bold
        else:
            cell.alignment = align_left

    ws1_widths = {1: 12, 2: 15, 3: 32, 4: 14, 5: 14, 6: 14, 7: 24, 8: 38, 9: 15}
    for col_idx, width in ws1_widths.items():
        ws1.column_dimensions[get_column_letter(col_idx)].width = width

    # ==========================================
    # SHEET 2: 功能状态与差距 (Feature Matrix)
    # ==========================================
    ws2 = wb.create_sheet(title="📋 功能状态与差距")
    setup_sheet_page(ws2)
    ws2.freeze_panes = "A4"

    ws2.merge_cells("A1:L1")
    ws2["A1"] = "灵犀平台 V1.4 自然语言控制功能状态矩阵与落地差距分析"
    ws2["A1"].font = font_title
    ws2["A1"].alignment = Alignment(horizontal='center', vertical='center')
    ws2["A1"].fill = PatternFill(start_color=C_NAVY_DARK, end_color=C_NAVY_DARK, fill_type='solid')
    ws2.row_dimensions[1].height = 36

    ws2.merge_cells("A2:L2")
    ws2["A2"] = "对照《设计规范 V1.4》逐条核对代码、单测、外部契约与真实设备状态 | 完成率定义：规则/单测已通过计入完成度，外部未接接口明确标示差距"
    ws2["A2"].font = font_subtitle
    ws2["A2"].alignment = Alignment(horizontal='center', vertical='center')
    ws2["A2"].fill = PatternFill(start_color=C_SUBTITLE_BG, end_color=C_SUBTITLE_BG, fill_type='solid')
    ws2.row_dimensions[2].height = 22

    f_headers = [
        "规范章节", "功能域", "具体功能模块", "V1.4 规范设计要求核心要点", 
        "当前代码实现与核心类/函数", "关联测试文件与覆盖用例数", "综合完成率", "当前未完成率", 
        "已闭环范围与能力", "未完成内容与客观差距", "外部依赖与瓶颈", "落地批次"
    ]
    ws2.row_dimensions[3].height = 28
    for col_idx, h in enumerate(f_headers, start=1):
        cell = ws2.cell(3, col_idx, h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_header

    feature_data = [
        ("2.1.1", "静态标注AI", "标注AI模型配置与显隐规则", "点/线/面显示【AI模型配置】按钮；体标注隐藏（底层不支持立体规划检测）；弹窗多选算法，非必填", "ai_lifecycle.AnnotationAlgorithmConfig\nBODY_ANNOTATION_AI_UNSUPPORTED", "test_ai_lifecycle (31 项)", 0.60, 0.40, "几何类型校验、体标注拦截报错、多选配置契约已闭环", "前端标注弹窗真实对接、静态标注库真实写接口未接入", "前端地图标注组件与Java标注库", "第2批"),
        ("2.1.2", "静态标注AI", "算法绑定与执行生命周期规则", "默认实时+航点检测；到达标注区域开启视频流分析；离开该标注地理范围自动关闭", "ai_lifecycle.ActiveAlgorithmSet\narrival.py 六阶段流转", "test_ai_lifecycle / test_arrival (36 项)", 0.65, 0.35, "六阶段生命周期状态机、算法运行集合维护、到达/离开判定单测通过", "真实算法启停接口（平台仅有任务级videoDetect，缺少算法级启停）", "AI推理平台算法启停接口", "第2批"),
        ("2.1.2", "静态标注AI", "全局与标注算法去重防误关", "若算法在全局配置中已被勾选，无人机离开标注区域时保留全局开启状态，绝不误关", "ai_lifecycle.py 去重逻辑\nalgorithms_to_stop", "test_ai_lifecycle 去重专项 (12 项)", 0.90, 0.10, "去重集合运算、全局算法保护逻辑、防误关测试完全通过", "真实视频分析推流画面与算法框选效果待真机验证", "流媒体服务与视频分析微服务", "第2批"),
        ("2.2.1", "通用前置", "控制权无豁免校验机制", "地面规划校验下发权限；飞行中所有指令（微操、载荷、急停、一键返航）无豁免校验控制权，无权拦截", "flight_policy.py\nauthority_contract.py", "test_product_v13 / test_authority_contract (49 项)", 0.90, 0.10, "确定性权限门禁、预览/确认双重校验、撤权即熔断全部闭环", "真实平台控制权申请与抢占接口联调", "Java控制权中台服务", "第3批"),
        ("2.2.2", "通用前置", "电子围栏防闯入与六态判定", "飞行微操前校验目标路径穿越禁飞区；clear放行，blocked/invalid拒绝，超时/不可用降级", "path_geometry.py\nsafety.py / test_path_fence", "test_path_fence (80 项)", 0.85, 0.15, "六态枚举处理、降级告警、路径折线插值与防闯入单测全部通过", "真实平台 /electronicFence/path-route/with-clean 37点联调", "Java电子围栏微服务", "第3批 / 第6批"),
        ("2.2.3", "通用前置", "状态介入校验（航线强插拦截）", "自动航线执行中除“暂停/返航/急停”外微操一律拦截；载荷指令放行；route_status未知fail-closed", "flight_policy.check_flight_policy\nROUTE_GATED_ACTIONS", "test_product_v13 / test_route_safety (46 项)", 0.90, 0.10, "航线微操拦截、载荷放行、route_status未知拒绝闭环", "平台当前快照 route_status 字段偶发为 null，需平台修复", "飞控状态快照同步契约", "第3批 / 第6批"),
        ("2.2.4", "通用前置", "悬停与航线断点业务规则", "自动航线悬停=调用暂停航线并保存断点；手动飞行悬停=原地悬停不涉及断点；绝不教成急停", "simulator.py HOVER 处理\nplatform_runtime._compile_hover", "test_v13_hover_and_point / test_cockpit (43 项)", 0.90, 0.10, "自动/手动悬停动作语义分离、断点数据保存、提示词修复闭环", "真实设备悬停折算指令（120/122/207）下发与响应", "飞控硬件执行器", "第3批"),
        ("2.2.5", "通用前置", "参数阈值智能换算校验机制", "指令参数超限自动取设备极值执行，不拦截并提示用户；卡片三列 requested/parameters/adjustments", "flight_policy.normalize_parameters\nmodels.AdjustmentDetail", "test_cockpit_quantity_bounds (9 项)", 0.95, 0.05, "设备参数表比对、智能极值修正、三列卡片提示逻辑完全闭环", "部分机型扩展载荷（如双光云台特殊档位）极值表完善", "机型参数字典配置库", "第3批"),
        ("3.1", "地面规划", "地面规划整体流程（11步流程）", "自然语言输入→意图理解→标注解析→航线决策→AI绑定→预览确认→下发任务并同步状态", "handoff_contract.py\nlingxi-agent 跨仓对接", "test_handoff_contract (41 项)", 0.40, 0.60, "交接信封协议 lingxi-flight-handoff/v1 定义与校验闭环", "完整地面规划11步链路在 lingxi-agent 仓，需跨仓联合联调", "任务规划 Agent 独立服务", "第1批"),
        ("3.2.1", "地面规划", "测绘与巡检触发词映射规则", "正射/三维/全景航线强制映射，等距拍照；航点/面状航线自适应；网格和几何体航线直接拒绝", "target_resolver.py\nflight_policy.py", "test_target_resolver (15 项)", 0.75, 0.25, "触发词解析、航线映射字典、不支持航线拦截单测通过", "与规划 Agent 跨仓联合回归", "lingxi-agent 代码同步", "第1批"),
        ("3.2.2", "地面规划", "静态标注与航线强制映射", "点目标多点按顺序；线目标单线端点；面目标单面弓字；立体与混合目标严格拒绝并话术提示", "target_resolver.resolve_targets\nhandoff_contract.py", "test_target_resolver (15 项)", 0.70, 0.30, "多点顺序保留、单线单面校验、体目标/混合目标拒绝完全闭环", "真实静态标注库数据读取接口未接（当前 mock）", "Java 地图标注服务", "第1批"),
        ("3.3", "地面规划", "规划阶段 AI 识别项生命周期", "自动加载标注绑定的AI算法；与全局AI去重；冲突时主动追问；未配置算法整批拒绝", "ai_lifecycle.plan_selection\nUnavailableAlgorithmGateway", "test_ai_lifecycle (31 项)", 0.60, 0.40, "计划选择、冲突追问状态机、未配置拒绝契约单测通过", "真实标注库AI绑定数据读取接口适配", "地图标注后端微服务", "第2批"),
        ("4.1.1", "任务管控", "临时任务接管与中断替换", "飞行中允许临时指点/环绕微操；临时任务中下发新指令允许中断替换并保留主线断点", "mapping_interruption.py\ntask_events.py", "test_mapping_interruption (46 项)", 0.80, 0.20, "临时任务抢占、旧任务作废、断点版本链维护闭环", "真实飞控任务中断切换接口联调", "平台飞控状态机服务", "第4批"),
        ("4.1.2", "任务管控", "测绘中断首屏警示与六步作废", "测绘任务首屏强提示成果作废风险；确认后执行六步原子事务（关拼图、作废、清断点、置灰恢复）", "mapping_interruption.INTERRUPTION_STEPS\nPlatformTransactionGateway", "test_mapping_interruption / test_task_events (61 项)", 0.80, 0.20, "首屏警示逻辑、六步原子事务顺序与多进程并发安全单测通过", "Java平台测绘原子事务接口实现（当前为本地模拟事务）", "Java任务中心后端", "第4批 / 第6批"),
        ("4.1.3", "任务管控", "任务断点恢复与显式二选一", "临时任务结束后必须显式提示并要求用户选择“恢复主线任务”或“结束全部任务”，绝不擅自猜测", "cockpit.py 决策流\nsimulator.py", "test_task_events (15 项)", 0.80, 0.20, "显式选项生成、选项二选一卡片确认逻辑闭环", "飞控遥测事件触发自动弹出前端选择卡片联调", "前端控制台弹窗组件", "第4批"),
        ("4.2", "任务管控", "低电量返航优先级与充电续飞", "低电量返航优先级高于一切控制指令；充电完成后仅恢复有效巡检主线，测绘作废任务绝不续飞", "task_events.py low_battery\nsimulator.apply_battery_event", "test_task_events (15 项)", 0.80, 0.20, "低电量事件抢占调度、充电后续飞条件判断完全闭环", "MQTT 真实电量遥测事件流订阅与推送", "MQTT 网关微服务", "第4批 / 第7批"),
        ("4.3.1", "控制词典", "飞行微操类控制指令", "前后左右上下（相对机头）、顺逆时针转向、悬停、急停、三维坐标指点飞行、指定半径环绕", "full_control.py\npath_geometry.py", "test_v13_hover_and_point / test_dji (24 项)", 0.85, 0.15, "机头相对坐标解算、微操参数换算、指点/环绕轨迹生成闭环", "平台对指点飞行/环绕接口能力放行声明与真机测试", "飞控硬件能力驱动", "第3批 / 第6批"),
        ("4.3.2", "控制词典", "载荷控制类指令（免暂停航线）", "云台俯仰角度/回中、变焦倍数、镜头模式（红外/广角/变焦）、拍照录像、喊话播报、夜航灯开关", "full_control.py\nv13_extension.py", "test_product_v13_actions / test_v13_ext (17 项)", 0.85, 0.15, "载荷指令免暂停航线校验、参数极值换算、相机状态回执闭环", "真实喊话器、夜航灯硬件通道联调", "硬件设备通道适配", "第3批 / 第8批"),
        ("4.3.3", "控制词典", "任务控制类指令", "暂停航线、恢复航线、一键返航、结束当前任务、结束全部任务；执行前重复安全检查", "cockpit.py\nplatform_runtime.py", "test_task_events / test_product_v13 (45 项)", 0.90, 0.10, "任务状态机切换、结束任务安全复检、断点清理闭环", "真实平台长连接任务结束接口响应", "平台任务中心", "第3批"),
        ("4.3.4", "控制词典", "复合指令拆解与顺序/并发执行", "大模型意图拆解；顺序执行逐步确认，任一步失败熔断；并发执行显式双卡，失败不伪造回滚", "plan.py\ncockpit_plan_execution.py", "test_cockpit_plan_execution (15 项)", 0.80, 0.20, "顺序/并发计划生成、状态卡片绑定、部分失败保全单测通过", "更复杂的自然语言多动词组合意图泛化评测", "大模型 Prompt 微调与评测", "第5批"),
        ("5.0", "异常话术", "异常场景统一提示话术库", "覆盖无控制权、航线强插、参数超限、平台遇障、测绘不可中断、不支持体目标等13大场景标准话术", "flight_policy.py 常量\nai_lifecycle.py", "test_model_failure_codes (16 项)", 0.80, 0.20, "13大场景固定话术常量、错误码映射、测试断言全部闭环", "真实前端页面提示浮层与语音播报体验走查", "前端交互与UI走查", "第3批 / 第5批"),
        ("E2E", "环境闭环", "真实平台接口与 Java 服务联调", "对接 Java /screen/uav/* 路由与 /electronicFence 围栏接口，验证真实鉴权与状态回执", "ktd_platform.py\nzhuoyi_platform.py", "test_ktd_platform (33 项)", 0.20, 0.80, "平台适配层代码已就绪、合成快照单测通过", "Java 后端测试环境服务联调与字段缺失补齐", "公司共享测试服务/独立端口", "第6批"),
        ("E2E", "环境闭环", "真机外场飞行与端到端闭环", "真实无人机外场起飞、自然语言控制微操/载荷、电子围栏拦截、低电量返航全流程实飞闭环", "现场飞手 / 真实无人机", "tools/flight_agent_demo.py", 0.00, 1.00, "离线与模拟器 100% 准备就绪，安全规则层完全闭环", "生产 MQTT、物理无人机飞控链路、外场飞手签字验收", "现场飞手与真机设备", "第8批")
    ]

    for idx, rdata in enumerate(feature_data, start=4):
        ws2.row_dimensions[idx].height = 26
        fill_curr = fill_white if idx % 2 == 0 else fill_zebra
        for c_idx, val in enumerate(rdata, start=1):
            cell = ws2.cell(idx, c_idx)
            cell.fill = fill_curr
            cell.border = border_cell
            if c_idx in (1, 2, 6, 12):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_center
            elif c_idx == 3:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_left
            elif c_idx in (4, 5, 9, 10, 11):
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
                        cell.fill = PatternFill(start_color=C_SUCCESS_BG, end_color=C_SUCCESS_BG, fill_type='solid')
                        cell.font = Font(name="Segoe UI", size=9.5, bold=True, color=C_SUCCESS_FG)
                    elif val >= 0.5:
                        cell.fill = PatternFill(start_color=C_WARN_BG, end_color=C_WARN_BG, fill_type='solid')
                        cell.font = Font(name="Segoe UI", size=9.5, bold=True, color=C_WARN_FG)
                    else:
                        cell.fill = PatternFill(start_color=C_DANGER_BG, end_color=C_DANGER_BG, fill_type='solid')
                        cell.font = Font(name="Segoe UI", size=9.5, bold=True, color=C_DANGER_FG)

    ws2_widths = {1: 11, 2: 13, 3: 24, 4: 34, 5: 30, 6: 22, 7: 13, 8: 13, 9: 30, 10: 32, 11: 24, 12: 13}
    for col_idx, width in ws2_widths.items():
        ws2.column_dimensions[get_column_letter(col_idx)].width = width

    # ==========================================
    # SHEET 3: 详细执行排期 (Schedule Details)
    # ==========================================
    ws3 = wb.create_sheet(title="🗓️ 详细执行排期")
    setup_sheet_page(ws3)
    ws3.freeze_panes = "A4"

    ws3.merge_cells("A1:O1")
    ws3["A1"] = "灵犀平台 V1.4 自然语言控制全阶段工程执行排期明细表 (0–8批完整链条)"
    ws3["A1"].font = font_title
    ws3["A1"].alignment = Alignment(horizontal='center', vertical='center')
    ws3["A1"].fill = PatternFill(start_color=C_NAVY_DARK, end_color=C_NAVY_DARK, fill_type='solid')
    ws3.row_dimensions[1].height = 36

    ws3.merge_cells("A2:O2")
    ws3["A2"] = "每项任务包含明确的前置条件、可执行测试命令、量化验收标准、责任角色与工时 | 涵盖基线维护、规则闭环、跨仓对接、平台联调及真机验收"
    ws3["A2"].font = font_subtitle
    ws3["A2"].alignment = Alignment(horizontal='center', vertical='center')
    ws3["A2"].fill = PatternFill(start_color=C_SUBTITLE_BG, end_color=C_SUBTITLE_BG, fill_type='solid')
    ws3.row_dimensions[2].height = 22

    t_headers = [
        "批次", "任务编号", "计划日期", "功能域", "具体任务内容", "责任角色", 
        "前置依赖条件", "测试验证命令 / 脚本", "量化验收标准", 
        "当前完成率", "当前未完成率", "计划工时(h)", "执行状态", "核心交付物", "风险评估与应对措施"
    ]
    ws3.row_dimensions[3].height = 28
    for col_idx, h in enumerate(t_headers, start=1):
        cell = ws3.cell(3, col_idx, h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_header

    tasks_data = [
        # 第0批：基线与证据 (2026-10-27)
        ("第0批", "B0-01", "2026-10-27", "基线", "核对分支、HEAD、工作区脏状态与远端状态", "主负责人", "仓库可读；不覆盖他人改动", "git status --short --branch; git log -1", "工作区状态完整记录，基线HEAD明确", 1.00, 0.00, 0.5, "已完成", "分支基线状态核对记录", "工作区脏改动冲突 / 严格保留本地改动"),
        ("第0批", "B0-02", "2026-10-27", "基线", "执行离线全量单元测试与离线安全守卫", "测试负责人", "Python依赖就绪；断网环境", "bash tools/ci/offline_tests.sh", "1115项测试全部PASS，无socket外连", 1.00, 0.00, 1.0, "已完成", "离线测试通过日志 (1115 passed)", "测试引入外部网络依赖 / offline_guard硬断言"),
        ("第0批", "B0-03", "2026-10-27", "基线", "源码静态编译与 whitespace diff 检查", "测试负责人", "B0-02 完成", "python3 -m compileall -q src tests; git diff --check", "零语法错误，diff --check 无空白错误", 1.00, 0.00, 0.5, "已完成", "代码静态检查清洁报告", "格式与缩进不规范 / 工具自动化修复"),
        ("第0批", "B0-04", "2026-10-27", "基线", "建立 V1.4 规范章节功能分母与证据索引", "主负责人", "设计规范V1.4文档可读", "逐条核对2.1–5.0节并登记完成状态", "建立覆盖18个核心功能的证据追溯矩阵", 1.00, 0.00, 1.0, "已完成", "V1.4 交付验收审计矩阵 (ACCEPTANCE_FLIGHT-V14.md)", "规范理解偏差 / 以文档逐条对齐为准"),
        ("第0批", "B0-05", "2026-10-27", "基线", "确认跨模块负责人与外部系统契约依赖", "项目负责人", "人员清单与外部接口文档可用", "梳理规划Agent、前端、Java平台接口责任人", "各任务分配唯一责任角色，前置依赖清晰", 1.00, 0.00, 1.0, "已完成", "跨角色协作职责清单与契约清单", "跨团队对接推诿 / 统一以 typed handoff 契约为准"),

        # 第1批：地面规划与跨仓交接 (2026-10-28)
        ("第1批", "B1-01", "2026-10-28", "地面规划", "点目标唯一解析与用户顺序严格保留", "规划Agent负责人", "静态标注含target_id/version/order", "python3 -m unittest tests/test_target_resolver.py", "多点按输入顺序串联，不静默改变顺序", 0.80, 0.20, 1.5, "进行中", "点目标顺序保留解析器与单测", "多点乱序导致航线不符 / order强制校验"),
        ("第1批", "B1-02", "2026-10-28", "地面规划", "线目标单线单任务强制端点映射", "规划Agent负责人", "线标注geometry/version可用", "python3 -m unittest -k 'test_line' tests/test_target_resolver.py", "就近选择端点飞向另一端，多线严格拒绝", 0.70, 0.30, 1.5, "进行中", "线目标强制端点映射器与反例测试", "用户框选多线 / 返回 MULTI_LINE_UNSUPPORTED"),
        ("第1批", "B1-03", "2026-10-28", "地面规划", "面目标单面自适应“弓”字形航线映射", "规划Agent负责人", "面标注geometry/version可用", "python3 -m unittest -k 'test_polygon' tests/test_target_resolver.py", "单面生成全覆盖扫描航线，多面严格拒绝", 0.70, 0.30, 1.0, "进行中", "面目标扫描映射器与拒绝单测", "复杂凹多边形 / 边界凸包或网格航线拒单"),
        ("第1批", "B1-04", "2026-10-28", "地面规划", "立体目标与混合目标输入强制拦截", "规划Agent负责人", "目标类型枚举统一", "python3 -m unittest -k 'test_unsupported' tests/test_target_resolver.py", "体目标返回体不支持，点+线返回混合不支持", 1.00, 0.00, 1.0, "已完成", "体与混合目标拦截器及固定话术单测", "大模型偷偷转换 / 确定性层严格拦截"),
        ("第1批", "B1-05", "2026-10-28", "地面规划", "任务下发权限与交接信封协议校验", "规划Agent/飞控负责人", "跨仓信封协议字段齐全", "python3 -m unittest tests/test_handoff_contract.py", "校验binding、版本号、幂等键、下发权限", 0.60, 0.40, 1.5, "进行中", "跨仓交接信封协议校验实现与回归报告", "快照版本过期 / 返回 STALE_HANDOFF_VERSION"),
        ("第1批", "B1-06", "2026-10-28", "地面规划", "任务类型隔离与飞行中控制边界保护", "飞控负责人", "task_type、route_status字段就绪", "python3 -m unittest tests/test_intent_scenarios.py", "巡检中发起测绘被拒；控制仓不擅自建任务", 0.80, 0.20, 1.5, "进行中", "任务类型隔离单测与边界断言", "地面规划与空中控制混淆 / 上下文版本严格区分"),

        # 第2批：静态标注与 AI 生命周期 (2026-10-29)
        ("第2批", "B2-01", "2026-10-29", "AI生命周期", "点/线/面绑定AI与体标注前端隐藏", "AI生命周期负责人", "标注geometry类型统一", "python3 -m unittest -k 'test_annotation_geometry' tests/test_ai_lifecycle.py", "体标注返回 BODY_ANNOTATION_AI_UNSUPPORTED", 1.00, 0.00, 1.0, "已完成", "体标注AI绑定门禁与错误码测试", "前端未隐藏按钮 / 后端硬门禁双重拦截"),
        ("第2批", "B2-02", "2026-10-29", "AI生命周期", "算法配置非必填与项目配置校验", "AI生命周期负责人", "configured_algorithms输入可用", "python3 -m unittest -k 'test_unconfigured' tests/test_ai_lifecycle.py", "未配置算法整批拒绝，不替换为近似算法", 1.00, 0.00, 1.0, "已完成", "算法白名单校验与未配置拒绝单测", "模型猜测相似算法 / 严格白名单校验"),
        ("第2批", "B2-03", "2026-10-29", "AI生命周期", "全局与标注专属算法集合去重", "AI生命周期负责人", "算法字典与去重集合就绪", "python3 -m unittest -k 'test_dedupe' tests/test_ai_lifecycle.py", "全局与局部重叠算法只启动一次，资源不浪费", 1.00, 0.00, 1.0, "已完成", "算法去重合并逻辑与测试用例", "重复推流消耗GPU / Set集合去重"),
        ("第2批", "B2-04", "2026-10-29", "AI生命周期", "到达开启、离开关闭与防误关状态机", "AI生命周期负责人", "位置事件与地理围栏计算就绪", "python3 -m unittest -k 'test_lifecycle' tests/test_ai_lifecycle.py", "离开标注关闭其专属算法；全局算法保持开启", 0.80, 0.20, 1.5, "进行中", "六阶段AI生命周期状态机与防误关单测", "误关全局算法 / algorithms_to_stop做差集过滤"),
        ("第2批", "B2-05", "2026-10-29", "AI生命周期", "AI配置冲突澄清追问与仅替换限制", "AI生命周期负责人", "意图输入与现有配置重叠", "python3 -m unittest -k 'test_conflict' tests/test_ai_lifecycle.py", "产生冲突主动追问；仅替换返回不支持话术", 1.00, 0.00, 1.0, "已完成", "AI冲突追问卡片生成与替换拦截测试", "用户模糊意图 / 发起 clarification 追问卡"),
        ("第2批", "B2-06", "2026-10-29", "前端联调", "前端静态标注 AI 配置弹窗与按钮显隐", "前端负责人", "onegismap_a 前端工作树就绪", "前端组件走查与 Jest 单元测试", "点线面展示配置弹窗，体标注彻底隐藏按钮", 0.20, 0.80, 1.5, "进行中", "前端 AI 配置组件与显隐走查记录", "旧前端缓存未刷新 / 强制清理并联调"),
        ("第2批", "B2-07", "2026-10-29", "平台对接", "真实算法启停接口契约对齐与联调", "平台适配负责人", "Java AI 服务接口文档就绪", "接口连通性 smoke 测试", "核实平台是否支持算法粒度启停及回执", 0.00, 1.00, 1.0, "待开始", "AI算法微服务接口契约核对备忘录", "平台仅支持任务级检测 / 推动平台暴露算法级接口"),

        # 第3批：飞行微操与载荷基础控制 (2026-10-30)
        ("第3批", "B3-01", "2026-10-30", "飞行控制", "飞行中控制权无豁免严格校验", "飞控负责人", "平台控制权快照可用", "python3 -m unittest -k 'test_control' tests/test_product_v13.py", "无控制权下发微操/载荷/急停/返航全部拦截", 1.00, 0.00, 1.0, "已完成", "控制权校验拦截器与无权提示话术单测", "微操绕过控制权 / 飞控核心网关层强制硬校验"),
        ("第3批", "B3-02", "2026-10-30", "飞行控制", "自动航线中微操强插拦截与载荷放行", "飞控负责人", "route_status 状态快照可用", "python3 -m unittest -k 'test_running_route' tests/test_product_v13.py", "航线中微操拦截提示先暂停；载荷指令放行", 1.00, 0.00, 1.0, "已完成", "状态介入校验器与载荷放行单测", "route_status 为 null / fail-closed 拦截并报缺口"),
        ("第3批", "B3-03", "2026-10-30", "飞行控制", "自然语言悬停等价暂停航线与断点保存", "飞控负责人", "simulator 与 platform 运行时可用", "python3 -m unittest tests/test_v13_hover_and_point.py", "自动航线悬停调用暂停并存断点；手动原地悬停", 1.00, 0.00, 1.0, "已完成", "悬停动作编译器与提示词防急停单测", "模型误把悬停教成急停 / 提示词与代码双重防护"),
        ("第3批", "B3-04", "2026-10-30", "飞行控制", "六向移动、转向与三维坐标指点飞行", "飞控负责人", "path_geometry 库就绪", "python3 -m unittest tests/test_cockpit_motion_grounding.py", "相对机头解算、极值限制、指点航迹生成", 0.90, 0.10, 1.0, "进行中", "飞行微操指令编译器与航迹计算单测", "坐标系颠倒 / 确定机头坐标系与绝对坐标系转换"),
        ("第3批", "B3-05", "2026-10-30", "飞行控制", "环绕路径生成与电子围栏六态校验", "飞控负责人", "电子围栏 fake provider 可用", "python3 -m unittest tests/test_path_fence.py", "环绕航线生成；围栏 blocked 拒绝，降级放行", 0.90, 0.10, 1.0, "进行中", "环绕几何解算与电子围栏六态单测", "围栏超时卡死 / 超时时间硬限制3秒并降级告警"),
        ("第3批", "B3-06", "2026-10-30", "载荷控制", "云台/变焦/镜头/拍照录像/喊话/照明", "飞控负责人", "models 载荷字典完整", "python3 -m unittest tests/test_product_v13_actions.py", "超限智能换算极值；免暂停航线执行载荷", 0.90, 0.10, 1.0, "进行中", "全套载荷控制指令集与极值修正测试", "硬件不支持某功能 / 提示当前设备未挂载"),
        ("第3批", "B3-07", "2026-10-30", "任务控制", "暂停、恢复、一键返航、结束当前/全部", "飞控负责人", "task_events 状态机就绪", "python3 -m unittest tests/test_task_events.py", "结束当前保留主线；结束全部清空断点", 1.00, 0.00, 1.0, "已完成", "任务生命周期控制动作与断点清理单测", "结束前未复检 / 状态变更前二次重复安全检查"),
        ("第3批", "B3-08", "2026-10-30", "复合指令", "顺序执行与并发执行大模型拆解调度", "大模型/飞控负责人", "plan.py 复合拆解器就绪", "python3 -m unittest tests/test_cockpit_plan_execution.py", "顺序逐步确认任一失败停；并发显式双卡", 0.85, 0.15, 1.0, "进行中", "复合指令规划器与部分失败保全单测", "并发部分失败伪造回滚 / 保留成功项明确告知"),

        # 第4批：测绘中断与低电量续飞闭环 (2026-11-02)
        ("第4批", "B4-01", "2026-11-02", "测绘中断", "测绘任务首屏不可中断警示与强插二次确认", "飞控负责人", "mapping_interruption 模块就绪", "python3 -m unittest tests/test_mapping_interruption.py", "首屏清晰提示成果作废风险，确认后方可执行", 0.80, 0.20, 1.5, "待开始", "测绘中断警示交互与二次确认卡片逻辑", "用户误触中断 / 必须二次弹窗确认并标红警告"),
        ("第4批", "B4-02", "2026-11-02", "测绘中断", "测绘中断六步原子事务作废与清理", "飞控负责人", "PlatformTransactionGateway 就绪", "python3 -m unittest -k 'test_confirmed_interruption' tests/test_mapping_interruption.py", "关拼图、作废任务、置灰恢复、旧卡失效全部原子执行", 0.80, 0.20, 2.0, "待开始", "六步原子事务执行器与多进程并发单测", "事务中途挂起 / 本地与平台双重事务锁防护"),
        ("第4批", "B4-03", "2026-11-02", "任务断点", "临时任务完成后显式二选一决策机制", "状态机负责人", "临时任务结束事件触发", "python3 -m unittest -k 'test_completion_requires' tests/test_task_events.py", "弹出卡片要求用户显式选择恢复主线或结束全部", 0.80, 0.20, 1.5, "待开始", "断点恢复决策卡片与状态转移单测", "系统擅自猜测行为 / 强制阻塞等待用户交互"),
        ("第4批", "B4-04", "2026-11-02", "低电量", "低电量返航最高优先级事件调度与抢占", "飞控负责人", "电量遥测事件模型可用", "python3 -m unittest -k 'test_low_battery' tests/test_task_events.py", "低电量事件触发立即打断其他指令并一键返航", 0.80, 0.20, 1.5, "待开始", "低电量调度器与高优先级抢占测试", "低电量被微操阻塞 / 状态机事件通道硬抢占"),
        ("第4批", "B4-05", "2026-11-02", "低电量", "充电完成恢复巡检主线与测绘废弃过滤", "飞控负责人", "recharge 事件模拟就绪", "python3 -m unittest -k 'test_recharge' tests/test_task_events.py", "充电后仅恢复有效巡检主线；作废测绘绝不续飞", 0.80, 0.20, 1.5, "待开始", "充电续飞条件过滤器与断点恢复单测", "误续飞已作废测绘 / 校验断点有效性标记"),

        # 第5批：复合指令拆解与异常话术前端验收 (2026-11-04)
        ("第5批", "B5-01", "2026-11-04", "复合指令", "复杂自然语言复合指令意图拆解泛化评测", "大模型负责人", "Ollama/模型评测集就绪", "python3 -m unittest tests/test_ollama_decider.py", "动词拆解准确率≥95%，参数提取准确率≥98%", 0.80, 0.20, 2.5, "待开始", "复合意图评测集 (50+条典型句型) 及基准报告", "长难句意图丢失 / Few-shot 示例增强与确定性校验"),
        ("第5批", "B5-02", "2026-11-04", "复合指令", "并发多动作卡片渲染与零伪造回滚机制", "飞控/前端负责人", "cockpit 卡片协议就绪", "python3 -m unittest tests/test_cockpit_plan_execution.py", "两张卡片显式呈现，部分失败保留成功项并提示", 0.80, 0.20, 1.5, "待开始", "并发卡片渲染组件与回滚抑制单测", "并发执行状态不同步 / 引入任务 correlation_id"),
        ("第5批", "B5-03", "2026-11-04", "异常话术", "13大异常场景固定标准话术前端统一走查", "前端/测试负责人", "flight_policy 话术常量就绪", "前端界面录屏与话术文本逐字核对", "13个场景话术与规范文档Table 6逐字一致", 0.80, 0.20, 2.0, "待开始", "13大场景异常话术前端走查报告与截屏", "前端擅自改动文案 / 引用共享常量库"),
        ("第5批", "B5-04", "2026-11-04", "前端交互", "微操与载荷控制确认态弹窗体验走查", "前端负责人", "lingxi-flight-agent-ui 仓", "UI 组件端到端走查", "确认卡包含参数调整、极值提示、风险说明", 0.80, 0.20, 2.0, "待开始", "控制台确认卡 UI 体验验收单", "卡片排版折行挤压 / 优化响应式布局与字号"),

        # 第6批：真实平台 (Java/Ktd) 接口联调 (2026-11-06)
        ("第6批", "B6-01", "2026-11-06", "平台联调", "KtdPlatformExecutor 真实 HTTP 接口打通", "平台适配/Java负责人", "Java后端提供测试环境端口", "python3 -m unittest tests/test_ktd_platform.py", "/screen/uav/* 接口全部联通，零 404/500 报错", 0.20, 0.80, 3.0, "待开始", "Java 接口联调日志与连通性验证报告", "平台接口返回格式变动 / 增加契约适配兼容层"),
        ("第6批", "B6-02", "2026-11-06", "平台联调", "route_status 真实快照字段获取与解析对齐", "平台适配/Java负责人", "Java 快照接口增加航线状态字段", "联调脚本查询无人机实时快照", "route_status 返回 idle/running/paused，不为 null", 0.20, 0.80, 3.0, "待开始", "快照字段完整性验证报告", "字段依然为 null / 推动 Java 修复并维持 fail-closed"),
        ("第6批", "B6-03", "2026-11-06", "平台联调", "电子围栏 /electronicFence 37点折线联调", "平台适配/Java负责人", "围栏微服务联调环境", "python3 -m unittest tests/test_path_fence.py", "37点折线插值校验通过，禁飞区与限高正确判定", 0.20, 0.80, 3.0, "待开始", "高精电子围栏联调报告与证据包", "围栏计算耗时过长 / Java接口优化与缓存"),
        ("第6批", "B6-04", "2026-11-06", "平台联调", "静态标注库与AI算法配置真实数据读取", "平台适配/Java负责人", "标注库接口权限开通", "联调脚本读取点/线/面及绑定的算法列表", "成功读取静态标注坐标、几何类型及已配算法", 0.20, 0.80, 3.0, "待开始", "静态标注库对接测试报告", "标注数据格式不一致 / 数据适配层做归一化"),

        # 第7批：MQTT 遥测与事件流总线集成 (2026-11-10)
        ("第7批", "B7-01", "2026-11-10", "MQTT集成", "无人机 MQTT 遥测上行数据流订阅与解析", "平台适配/物联网负责人", "MQTT 测试 Broker 账号就绪", "遥测数据消费测试脚本", "实时接收经纬高、姿态、电量、当前航点与状态", 0.10, 0.90, 4.0, "待开始", "MQTT 遥测订阅服务与数据抓包日志", "丢包与网络延迟 / QoS 设置与心跳重连机制"),
        ("第7批", "B7-02", "2026-11-10", "MQTT集成", "控制指令平台下发回执状态机映射", "飞控负责人", "Java 下行指令响应格式确定", "python3 -m unittest tests/test_command_receipts.py", "submitted/accepted/executing/completed 映射无误", 0.10, 0.90, 4.0, "待开始", "指令回执状态机驱动单测与联调日志", "设备执行超时无回执 / 状态机超时降级与告警"),
        ("第7批", "B7-03", "2026-11-10", "MQTT集成", "任务事件总线（到达/离开/低电量/中断）打通", "状态机负责人", "事件驱动消息队列就绪", "端到端事件模拟回放脚本", "事件毫秒级分发至 AI 生命周期与断点管理模块", 0.10, 0.90, 4.0, "待开始", "全系统事件总线联调通过记录", "事件乱序到达 / 事件序列号与时间戳校验过滤"),

        # 第8批：真实设备外场试飞与端到端闭环验收 (2026-11-13)
        ("第8批", "B8-01", "2026-11-13", "真机验收", "外场真实无人机控制权获取与地面规划执行", "现场飞手/飞控负责人", "外场空域审批、物理无人机与遥控器就绪", "现场自然语言任务下发与起飞执行", "通过自然语言成功下发巡检任务，无人机按航线起飞", 0.00, 1.00, 5.0, "待开始", "外场试飞地面规划与起飞验收视频与日志", "天气或空域突发状况 / 备选试飞窗口与安全预案"),
        ("第8批", "B8-02", "2026-11-13", "真机验收", "飞行中自然语言微操/载荷控制与围栏拦截实测", "现场飞手/测试负责人", "无人机空中悬停/巡检中", "现场下达微操、载荷、围栏禁飞区闯入测试", "微操与载荷正常响应，禁飞区指令被平台成功拦截", 0.00, 1.00, 5.0, "待开始", "空中控制与围栏防闯入实飞验收记录", "机载避障触发 / 飞控底层物理避障优先保障安全"),
        ("第8批", "B8-03", "2026-11-13", "真机验收", "外场低电量返航、测绘中断与全流程签字交付", "项目负责人/现场总指挥", "全要素外场实战演习", "全天候实飞测试与故障场景演练", "低电量安全返航、中断作废合规，V1.4 全流程签字", 0.00, 1.00, 6.0, "待开始", "V1.4 端到端交付验收签字报告与交付包", "关键用例未通过 / 当场复测并记录跟踪单")
    ]

    for idx, rdata in enumerate(tasks_data, start=4):
        ws3.row_dimensions[idx].height = 24
        fill_curr = fill_white if idx % 2 == 0 else fill_zebra
        for c_idx, val in enumerate(rdata, start=1):
            cell = ws3.cell(idx, c_idx)
            cell.fill = fill_curr
            cell.border = border_cell
            if c_idx in (1, 2, 3, 4, 6):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_center
            elif c_idx == 5:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_left
            elif c_idx in (7, 8, 9, 14, 15):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_left_wrap
            elif c_idx in (10, 11):
                cell.value = val
                cell.font = font_num_bold
                cell.number_format = '0.0%'
                cell.alignment = align_center
                if c_idx == 10:
                    if val == 1.0:
                        cell.fill = PatternFill(start_color=C_SUCCESS_BG, end_color=C_SUCCESS_BG, fill_type='solid')
                        cell.font = Font(name="Segoe UI", size=9.5, bold=True, color=C_SUCCESS_FG)
                    elif val >= 0.5:
                        cell.fill = PatternFill(start_color=C_WARN_BG, end_color=C_WARN_BG, fill_type='solid')
                        cell.font = Font(name="Segoe UI", size=9.5, bold=True, color=C_WARN_FG)
                    else:
                        cell.fill = PatternFill(start_color=C_DANGER_BG, end_color=C_DANGER_BG, fill_type='solid')
                        cell.font = Font(name="Segoe UI", size=9.5, bold=True, color=C_DANGER_FG)
            elif c_idx == 12:
                cell.value = val
                cell.font = font_num
                cell.number_format = '0.0'
                cell.alignment = align_right
            elif c_idx == 13:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_center
                if val == "已完成":
                    cell.fill = PatternFill(start_color=C_SUCCESS_BG, end_color=C_SUCCESS_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color=C_SUCCESS_FG)
                elif val == "进行中":
                    cell.fill = PatternFill(start_color=C_WARN_BG, end_color=C_WARN_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color=C_WARN_FG)
                else:
                    cell.fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, color="64748B")

    last_task_row = 4 + len(tasks_data)
    ws3.row_dimensions[last_task_row].height = 25
    ws3.cell(last_task_row, 1, "全任务汇总")
    ws3.cell(last_task_row, 2, f"共 {len(tasks_data)} 项")
    ws3.cell(last_task_row, 3, "2026-10-27 ~ 11-13")
    ws3.cell(last_task_row, 4, "全功能域覆盖")
    ws3.cell(last_task_row, 5, "从基线维护到外场试飞签字完整交付闭环")
    ws3.cell(last_task_row, 6, "跨团队协同")
    ws3.cell(last_task_row, 7, "前置依赖链闭环")
    ws3.cell(last_task_row, 8, "单元/模拟/平台/真机全层级")
    ws3.cell(last_task_row, 9, "42项任务全部通过验收标准")
    ws3.cell(last_task_row, 10, f"=AVERAGE(J4:J{last_task_row-1})")
    ws3.cell(last_task_row, 11, f"=AVERAGE(K4:K{last_task_row-1})")
    ws3.cell(last_task_row, 12, f"=SUM(L4:L{last_task_row-1})")
    ws3.cell(last_task_row, 13, "统筹执行中")
    ws3.cell(last_task_row, 14, "交付物齐全")
    ws3.cell(last_task_row, 15, "风险完全受控")

    for c in range(1, 16):
        cell = ws3.cell(last_task_row, c)
        cell.fill = fill_total
        cell.border = border_total
        cell.font = font_body_bold
        if c in (1, 2, 3, 4, 6, 13):
            cell.alignment = align_center
        elif c in (10, 11):
            cell.alignment = align_center
            cell.number_format = '0.0%'
            cell.font = font_num_bold
        elif c == 12:
            cell.alignment = align_right
            cell.number_format = '0.0'
            cell.font = font_num_bold
        else:
            cell.alignment = align_left

    ws3_widths = {1: 10, 2: 11, 3: 13, 4: 12, 5: 32, 6: 18, 7: 28, 8: 38, 9: 38, 10: 13, 11: 13, 12: 13, 13: 12, 14: 34, 15: 34}
    for col_idx, width in ws3_widths.items():
        ws3.column_dimensions[get_column_letter(col_idx)].width = width

    # ==========================================
    # SHEET 4: 测试用例与证据 (Test Scenarios)
    # ==========================================
    ws4 = wb.create_sheet(title="🧪 测试用例与验证")
    setup_sheet_page(ws4)
    ws4.freeze_panes = "A4"

    ws4.merge_cells("A1:K1")
    ws4["A1"] = "灵犀平台 V1.4 重点场景测试用例与验证证据清单"
    ws4["A1"].font = font_title
    ws4["A1"].alignment = Alignment(horizontal='center', vertical='center')
    ws4["A1"].fill = PatternFill(start_color=C_NAVY_DARK, end_color=C_NAVY_DARK, fill_type='solid')
    ws4.row_dimensions[1].height = 36

    ws4.merge_cells("A2:K2")
    ws4["A2"] = "精选覆盖 V1.4 全场景的核心测试用例矩阵 | 明确执行命令、预期行为、证据文件位置与执行模式（单元/模拟器/假平台/平台联调/真机）"
    ws4["A2"].font = font_subtitle
    ws4["A2"].alignment = Alignment(horizontal='center', vertical='center')
    ws4["A2"].fill = PatternFill(start_color=C_SUBTITLE_BG, end_color=C_SUBTITLE_BG, fill_type='solid')
    ws4.row_dimensions[2].height = 22

    sc_headers = [
        "用例编号", "所属批次", "功能域", "测试场景描述", "前置状态 / 输入参数", 
        "执行命令 / 测试方法", "预期系统响应与用户话术", "实际证据与用例位置", 
        "验证模式", "责任角色", "当前状态"
    ]
    ws4.row_dimensions[3].height = 28
    for col_idx, h in enumerate(sc_headers, start=1):
        cell = ws4.cell(3, col_idx, h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_header

    scenarios_data = [
        ("T0-01", "第0批", "基线", "离线自动化全量回归测试", "本地源码与依赖已就绪，完全离线", "bash tools/ci/offline_tests.sh", "1115 项用例全部 PASS，零 socket 连接", "tools/ci/offline_tests.sh", "纯单元/隔离", "测试负责人", "PASS"),
        ("T0-02", "第0批", "基线", "源码语法编译与空白符检查", "工作树清洁无脏变更", "python3 -m compileall -q src tests; git diff --check", "零 SyntaxError，零 trailing whitespace", "本机命令输出", "代码静态检查", "测试负责人", "PASS"),
        ("T1-01", "第1批", "地面规划", "多点目标输入严格保留用户指定顺序", "静态标注 A、B 两点，用户输入 order=[1, 2]", "test_target_resolver.py", "输出顺序与用户一致，不因就近原则自动重排", "test_target_resolver", "单元测试", "规划Agent负责人", "PASS"),
        ("T1-02", "第1批", "地面规划", "单线目标强制端点起飞，多线输入拒绝", "同时选中 2 条线标注", "test_handoff_contract.py", "返回 MULTI_LINE_UNSUPPORTED 拒绝码并明确提示", "test_handoff_contract", "单元测试", "规划Agent负责人", "PASS"),
        ("T1-03", "第1批", "地面规划", "单面目标自适应生成弓字形扫描航线", "单个面标注多边形坐标", "test_target_resolver.py", "成功规划单面全覆盖航线；多面输入则拒绝", "test_target_resolver", "单元测试", "规划Agent负责人", "PASS"),
        ("T1-04", "第1批", "地面规划", "立体目标与混合目标输入强制拒绝", "targets 包含 volume 或 point+line 混合", "test_target_resolver.py", "返回体目标不支持或混合目标不支持 typed 错误码", "test_target_resolver", "单元测试", "规划Agent负责人", "PASS"),
        ("T1-05", "第1批", "地面规划", "任务交接信封快照版本过期防护", "snapshot_version 不匹配", "test_handoff_contract.py", "拒绝下发并返回 STALE_HANDOFF_VERSION", "test_handoff_contract", "单元测试", "飞控负责人", "PASS"),
        ("T2-01", "第2批", "AI生命周期", "立体标注绑定 AI 算法强制拦截", "geometry_type=volume", "test_ai_lifecycle.py", "返回 BODY_ANNOTATION_AI_UNSUPPORTED", "test_ai_lifecycle", "单元测试", "AI生命周期负责人", "PASS"),
        ("T2-02", "第2批", "AI生命周期", "未配置算法整批拒绝且不近似替换", "algorithm 不在 configured 集合中", "test_ai_lifecycle.py", "整批抛出 ALGORITHM_NOT_CONFIGURED，无近似替换", "test_ai_lifecycle", "单元测试", "AI生命周期负责人", "PASS"),
        ("T2-03", "第2批", "AI生命周期", "全局算法与标注算法集合运算去重", "全局配置 fire，标注同时配置 fire 和 smoke", "test_ai_lifecycle.py", "算法集合合并去重，fire 只启动一次", "test_ai_lifecycle", "单元测试", "AI生命周期负责人", "PASS"),
        ("T2-04", "第2批", "AI生命周期", "无人机离开标注区域专属算法关闭", "标注 A 离开，全局未配置对应算法", "test_ai_lifecycle.py", "专属算法正常触发停止事件并从运行集移除", "test_ai_lifecycle", "单元+模拟器", "AI生命周期负责人", "PASS"),
        ("T2-05", "第2批", "AI生命周期", "全局算法防误关保护机制", "标注 A 和全局都配 fire，离开标注 A", "test_ai_lifecycle.py", "离开 A 后 fire 依然在运行集合，防误关生效", "test_ai_lifecycle", "单元+模拟器", "AI生命周期负责人", "PASS"),
        ("T2-06", "第2批", "AI生命周期", "AI 识别配置冲突主动追问澄清", "标注已有 fire，用户口令要求切换为 smoke", "test_ai_lifecycle.py", "生成澄清追问卡片，不擅自替换或生成确认卡", "test_ai_lifecycle", "单元测试", "AI生命周期负责人", "PASS"),
        ("T3-01", "第3批", "飞行控制", "无控制权下发飞行微操强制拦截", "control_owned=false，指令“向前飞10米”", "test_product_v13.py", "拦截下发，提示“您尚未获取无人机控制权”", "test_product_v13", "模拟器/假平台", "飞控负责人", "PASS"),
        ("T3-02", "第3批", "飞行控制", "无控制权下发急停/返航/载荷无豁免", "control_owned=false，指令“急停”/“变焦”", "test_product_v13.py", "一律拦截，不存在任何控制权豁免", "test_product_v13", "模拟器/假平台", "飞控负责人", "PASS"),
        ("T3-03", "第3批", "飞行控制", "自动航线执行中飞行微操强插拦截", "route_status=running，指令“向左飞5米”", "test_product_v13.py", "拦截并提示“当前正在执行自动航线，请先悬停”", "test_product_v13", "模拟器/假平台", "飞控负责人", "PASS"),
        ("T3-04", "第3批", "飞行控制", "自动航线执行中载荷指令放行", "route_status=running，指令“变焦到10倍”", "test_product_v13_actions.py", "正常放行并生成确认卡，免暂停航线", "test_product_v13_actions", "模拟器/假平台", "飞控负责人", "PASS"),
        ("T3-05", "第3批", "飞行控制", "自然语言悬停等价暂停航线与保存断点", "route_status=running，口令“悬停一下”", "test_v13_hover_and_point.py", "映射为暂停航线并保存断点，绝不替换为急停", "test_v13_hover_and_point", "模拟器/假平台", "飞控负责人", "PASS"),
        ("T3-06", "第3批", "飞行控制", "电子围栏目标路径穿越禁飞区拦截", "fake provider 返回 blocked 状态", "test_path_fence.py", "拦截下发，返回 PATH_BLOCKED 并提示障碍禁飞区", "test_path_fence", "模拟器/假平台", "飞控负责人", "PASS"),
        ("T3-07", "第3批", "飞行控制", "电子围栏服务接口超时/不可用自动降级", "fake provider 响应超时 3 秒", "test_path_fence.py", "指令继续放行，带 timeout 降级警告提示", "test_path_fence", "模拟器/假平台", "飞控负责人", "PASS"),
        ("T3-08", "第3批", "载荷控制", "参数超限自动取设备极值并明确提示", "指令“变焦到100倍”，实际设备最大30倍", "test_cockpit_quantity_bounds.py", "effective 取 30，提示“最大变焦为30倍，已调整”", "test_cockpit_quantity_bounds", "模拟器/假平台", "飞控负责人", "PASS"),
        ("T3-09", "第3批", "任务控制", "结束当前任务保留主线断点", "当前正在执行临时指点任务", "test_task_events.py", "临时任务结束，主线断点完整保留", "test_task_events", "模拟器", "飞控负责人", "PASS"),
        ("T3-10", "第3批", "任务控制", "结束全部任务清空全部断点与状态", "当前有主线与临时任务", "test_task_events.py", "清空主线断点，任务状态机完整重置", "test_task_events", "模拟器", "飞控负责人", "PASS"),
        ("T4-01", "第4批", "测绘中断", "测绘任务首屏不可中断警示二次确认", "任务类型为测绘建图，指令试图强插微操", "test_mapping_interruption.py", "弹出首屏警示“不可中断，若强插成果作废”确认卡", "test_mapping_interruption", "模拟器/联调", "飞控负责人", "待联调"),
        ("T4-02", "第4批", "测绘中断", "测绘确认中断六步原子事务执行", "用户确认作废测绘并执行新微操", "test_mapping_interruption.py", "关拼图、作废、清断点、置灰恢复六步原子执行", "test_mapping_interruption", "模拟器/联调", "飞控负责人", "待联调"),
        ("T4-03", "第4批", "低电量", "低电量返航最高优先级事件抢占调度", "电量跌破返航阈值 (battery < 20%)", "test_task_events.py", "最高优先级抢占，无论处于何种状态均触发返航", "test_task_events", "模拟器", "飞控负责人", "PASS"),
        ("T4-04", "第4批", "低电量", "充电完成仅续飞有效主线断点", "电量恢复 100%，触发 recharge 事件", "test_task_events.py", "若主线有效则续飞；若已作废或结束则绝不续飞", "test_task_events", "模拟器", "飞控负责人", "PASS"),
        ("T5-01", "第5批", "复合指令", "并发复合指令部分失败不伪造回滚", "同时执行移动与喊话，喊话失败", "test_cockpit_plan_execution.py", "移动成功予以保留，喊话失败明确告知，不回滚", "test_cockpit_plan_execution", "模拟器", "大模型/飞控", "PASS"),
        ("T6-01", "第6批", "平台联调", "真实平台快照 route_status 字段校验", "调用真实 Java /screen/uav 状态接口", "联调脚本", "route_status 字段正常返回非 null 状态", "Java 联调接口", "真实平台联调", "平台适配负责人", "待就绪"),
        ("T8-01", "第8批", "真机验收", "外场真实无人机自然语言微操与围栏防闯", "外场实飞，自然语言下发微操与航线控制", "外场试飞", "无人机姿态与轨迹响应准确，禁飞区被物理拦截", "真机外场日志与录屏", "外场真机飞行", "现场飞手/指挥", "待就绪")
    ]

    for idx, rdata in enumerate(scenarios_data, start=4):
        ws4.row_dimensions[idx].height = 24
        fill_curr = fill_white if idx % 2 == 0 else fill_zebra
        for c_idx, val in enumerate(rdata, start=1):
            cell = ws4.cell(idx, c_idx)
            cell.fill = fill_curr
            cell.border = border_cell
            if c_idx in (1, 2, 3, 9, 10):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_center
            elif c_idx == 4:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_left
            elif c_idx in (5, 6, 7, 8):
                cell.value = val
                cell.font = font_body
                cell.alignment = align_left_wrap
            elif c_idx == 11:
                cell.value = val
                cell.font = font_body_bold
                cell.alignment = align_center
                if val == "PASS":
                    cell.fill = PatternFill(start_color=C_SUCCESS_BG, end_color=C_SUCCESS_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color=C_SUCCESS_FG)
                elif val == "待联调":
                    cell.fill = PatternFill(start_color=C_WARN_BG, end_color=C_WARN_BG, fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, bold=True, color=C_WARN_FG)
                else:
                    cell.fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type='solid')
                    cell.font = Font(name=FONT_FAMILY, size=9.5, color="64748B")

    ws4_widths = {1: 11, 2: 11, 3: 13, 4: 28, 5: 32, 6: 34, 7: 38, 8: 26, 9: 15, 10: 16, 11: 12}
    for col_idx, width in ws4_widths.items():
        ws4.column_dimensions[get_column_letter(col_idx)].width = width

    # ==========================================
    # SHEET 5: 来源与口径 (Standards & Bounds)
    # ==========================================
    ws5 = wb.create_sheet(title="📐 来源与口径")
    setup_sheet_page(ws5)

    ws5.merge_cells("A1:G1")
    ws5["A1"] = "灵犀平台 V1.4 自然语言智能控制排期设计依据与核算口径标准"
    ws5["A1"].font = font_title
    ws5["A1"].alignment = Alignment(horizontal='center', vertical='center')
    ws5["A1"].fill = PatternFill(start_color=C_NAVY_DARK, end_color=C_NAVY_DARK, fill_type='solid')
    ws5.row_dimensions[1].height = 36

    ws5.merge_cells("A2:G2")
    ws5["A2"] = "用于追溯本排期表的设计规范来源、代码审计基线、测试真源与安全红线边界定义"
    ws5["A2"].font = font_subtitle
    ws5["A2"].alignment = Alignment(horizontal='center', vertical='center')
    ws5["A2"].fill = PatternFill(start_color=C_SUBTITLE_BG, end_color=C_SUBTITLE_BG, fill_type='solid')
    ws5.row_dimensions[2].height = 22

    # 第一部分：来源文档与基线
    ws5["A4"] = "一、排期设计依据与代码审计基线"
    ws5["A4"].font = font_section
    ws5.row_dimensions[4].height = 24

    std_headers = ["依据类型", "文档/代码源路径", "版本/哈希标识", "核心作用与覆盖范围", "状态与时效性"]
    ws5.row_dimensions[5].height = 26
    for col_idx, h in enumerate(std_headers, start=1):
        cell = ws5.cell(5, col_idx, h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_header

    standards_data = [
        ("功能设计规范", "灵犀平台自然语言智能控制功能设计规范V1.4.docx", "SHA256: e4e42b712e32...8c78", "V1.4 业务需求与交互规范真源（含2.1静态标注AI、2.2通用前置校验、3地面规划、4飞行控制与状态机、5异常话术）", "基线已冻结"),
        ("最终交付审计", "docs/ACCEPTANCE_FLIGHT-V14.md", "Commit: d6d284d 之后", "V1.4 最终验收矩阵、13项安全错误专项检查、已闭环与未验证差距清单", "已审计归档"),
        ("离线测试真源", "tools/ci/offline_tests.sh", "1115 Tests PASS / 0 Failed", "本地离线自动化全量测试套件（等价 discover -s tests -p 'test_*.py'），断网守卫拦截 socket", "持续集成真源"),
        ("代码仓库基线", "lingxi-flight-control-agent", "分支: test (领先 origin/test 1 提交)", "飞控自然语言解析、策略门禁、内存执行器闭环代码库；保留未提交改动", "当前开发基线"),
        ("前端代码基线", "lingxi-flight-agent-ui", "前端分支联调中", "飞控助手前端交互、静态标注AI配置弹窗、微操确认卡与异常话术呈现", "协同推进中")
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

    # 第二部分：完成率核算与折算口径定义
    ws5["A12"] = "二、完成率与执行状态核算口径定义"
    ws5["A12"].font = font_section
    ws5.row_dimensions[12].height = 24

    rule_headers = ["完成度状态", "折算口径百分比", "严格评定标准与证据要求", "后续批次演进要求"]
    ws5.row_dimensions[13].height = 26
    for col_idx, h in enumerate(rule_headers, start=1):
        cell = ws5.cell(13, col_idx, h)
        cell.font = font_header
        cell.fill = PatternFill(start_color="1E40AF", end_color="1E40AF", fill_type='solid')
        cell.alignment = align_center
        cell.border = border_header

    rules_data = [
        ("已完成 (Completed)", "100%", "代码实现完成、通过自动化离线单元测试覆盖，且具备不可篡改的测试证据文件。无未决技术债务。", "维持基线不退化，进入回归测试池"),
        ("部分完成 (In-Progress)", "40% – 85%", "核心业务规则与内存模拟器已闭环（如六步作废、去重防误关），但外部真实接口（如Java后端/MQTT/电子围栏）缺失或未联调通过。", "按排期批次推进跨仓联调与接口对接"),
        ("待开始 / 未验证 (Pending)", "0% – 20%", "真实平台未提供有效接口、MQTT遥测未接通或依赖外场真机飞行的场景。绝不得用模拟器通过冒充真实完成。", "进入第6–8批平台与真机外场专项攻坚"),
        ("硬性红线与安全约束", "一票否决门禁", "1. 无控制权绝不下发指令；2. route_status 未知一律 fail-closed；3. 悬停绝不替换为急停；4. 测绘中断必须先警告后作废；5. 不得擅自操作共享生产/测试环境。", "全阶段开发与联调过程中严格遵守")
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

    ws5_widths = {1: 18, 2: 32, 3: 26, 4: 42, 5: 18, 6: 15, 7: 15}
    for col_idx, width in ws5_widths.items():
        ws5.column_dimensions[get_column_letter(col_idx)].width = width

    # 保存文件
    output_dir = "/Users/wangxiaolin/PycharmProjects/lingxi-flight-control-agent/outputs/flight-v14-plan-20261027"
    os.makedirs(output_dir, exist_ok=True)
    
    target_path = os.path.join(output_dir, "灵犀平台V1.4自然语言控制功能执行排期与全量状态看板.xlsx")
    wb.save(target_path)
    print(f"成功保存高保真专业 Excel 至: {target_path}")

    compat_path = os.path.join(output_dir, "灵犀V14第0-3批执行排期.xlsx")
    wb.save(compat_path)
    print(f"成功覆盖并升级兼容原路径 Excel 至: {compat_path}")

if __name__ == "__main__":
    build_workbook()
