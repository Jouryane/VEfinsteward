"""
VE4 策略代码生成 Agent
========================
职责：将用户的自然语言策略描述转化为可执行的 Python 代码。

输入：自然语言描述（如"沪深300回撤5%买入，回测胜率"）
输出：可直接在 CodeSandbox 执行的 Python 代码

LLM 路由：tactical_code_gen → cloud_beta（非隐私，策略思路是公开描述）
"""

import logging
from datetime import datetime
from typing import Optional

from tactical.shared.base_agent import VE4TacticalAgent
from tactical.shared.models.tactical_models import (
    VE4AgentTask,
    VE4AgentResult,
    VE4AgentStatus,
)

logger = logging.getLogger("ve4.tactical.code_generator")


# ════════════════════════════════════════════════════════════════
# CodeGeneratorAgent
# ════════════════════════════════════════════════════════════════

class VE4CodeGeneratorAgent(VE4TacticalAgent):
    """
    策略代码生成 Agent

    流程：
        1. 接收用户自然语言描述
        2. 构建代码生成 prompt（包含数据源可用性、代码规范）
        3. 调用 LLM（cloud_beta）生成 Python 代码
        4. 对代码进行基本校验（语法检查、安全性扫描）
        5. 返回可执行代码
    """

    agent_type = "code_generator"

    # ── 核心接口 ──

    async def execute(self, task: VE4AgentTask) -> VE4AgentResult:
        """执行代码生成任务"""
        self.set_status(VE4AgentStatus.EXECUTING)
        start = datetime.now()

        try:
            params = task.params or {}
            prompt = params.get("prompt", "")

            if not prompt:
                return self.make_result(task, False, error="缺少策略描述")

            # 生成代码
            code = await self._generate_code(prompt)

            # 基础校验
            validation = self._validate_code(code)
            if not validation["valid"]:
                return self.make_result(task, False, error=f"代码校验失败: {validation['error']}")

            duration = int((datetime.now() - start).total_seconds() * 1000)
            return self.make_result(task, True, data={
                "code": code,
                "prompt": prompt,
                "validation": validation,
            }, duration_ms=duration)

        except Exception as e:
            logger.error(f"[CodeGenerator] 执行失败: {e}", exc_info=True)
            return self.make_result(task, False, error=str(e))
        finally:
            self.reset()

    # ── 代码生成 ──

    async def _generate_code(self, prompt: str) -> str:
        """
        基于 prompt 生成 Python 策略代码。

        当前实现：使用内置模板 + 规则匹配（开发测试模式）。
        TODO: 接入 LLM（cloud_beta）进行真正的自然语言到代码转换。
        """
        # TODO: 接入 LLM 调用
        # 当前使用启发式规则生成代码
        return self._rule_based_generate(prompt)

    def _rule_based_generate(self, prompt: str) -> str:
        """基于规则的代码生成（LLM 接入前的 fallback）"""
        prompt_lower = prompt.lower()

        # 沪深300 + 回撤 + 买入
        if any(k in prompt_lower for k in ["沪深300", "hs300", "回撤", "下跌", "跌幅"]):
            return self._generate_drawdown_backtest_code(prompt)

        # 均线
        if any(k in prompt_lower for k in ["均线", "ma", "moving average"]):
            return self._generate_ma_strategy_code(prompt)

        # RSI
        if any(k in prompt_lower for k in ["rsi", "超买", "超卖"]):
            return self._generate_rsi_strategy_code(prompt)

        # 默认：通用回测框架
        return self._generate_generic_backtest_code(prompt)

    def _generate_drawdown_backtest_code(self, prompt: str) -> str:
        """生成回撤买入策略回测代码"""
        return '''import akshare as ak
import pandas as pd
import numpy as np

# 获取沪深300历史数据
df = ak.index_zh_a_hist(symbol="000300", period="daily", start_date="20190101", end_date="20241231")
df['日期'] = pd.to_datetime(df['日期'])
df = df.sort_values('日期').reset_index(drop=True)
df['close'] = df['收盘'].astype(float)

# 计算3日回撤
df['high_3d'] = df['close'].rolling(3).max()
df['drawdown'] = (df['close'] - df['high_3d']) / df['high_3d'] * 100

# 信号：3日内回撤超5%，第三天收盘价买入
signals = []
holdings = []
returns = []

for i in range(3, len(df)):
    # 检查前3天内的回撤
    recent = df.iloc[i-2:i+1]
    recent_high = recent['close'].max()
    current = df.iloc[i]
    dd = (current['close'] - recent_high) / recent_high * 100

    if dd <= -5:
        # 买入信号
        buy_price = current['close']
        buy_date = current['日期']

        # 模拟持有30天
        sell_idx = min(i + 30, len(df) - 1)
        sell_price = df.iloc[sell_idx]['close']
        ret = (sell_price - buy_price) / buy_price * 100

        signals.append({
            'buy_date': buy_date.strftime('%Y-%m-%d'),
            'buy_price': round(buy_price, 2),
            'sell_price': round(sell_price, 2),
            'return_pct': round(ret, 2),
            'holding_days': sell_idx - i,
        })

signals_df = pd.DataFrame(signals)

if len(signals_df) > 0:
    win_rate = (signals_df['return_pct'] > 0).mean() * 100
    avg_return = signals_df['return_pct'].mean()
    max_return = signals_df['return_pct'].max()
    min_return = signals_df['return_pct'].min()

    print("=" * 50)
    print("策略回测结果：沪深300回撤5%买入策略")
    print("=" * 50)
    print(f"总信号次数: {len(signals_df)}")
    print(f"胜率: {win_rate:.1f}%")
    print(f"平均收益率: {avg_return:.2f}%")
    print(f"最大单笔收益: {max_return:.2f}%")
    print(f"最大单笔亏损: {min_return:.2f}%")
    print("\\n最近5次交易:")
    print(signals_df.tail(5).to_string(index=False))
else:
    print("未触发任何交易信号")
'''

    def _generate_ma_strategy_code(self, prompt: str) -> str:
        """生成均线策略代码"""
        return '''import akshare as ak
import pandas as pd

# 获取数据
df = ak.index_zh_a_hist(symbol="000300", period="daily", start_date="20230101", end_date="20241231")
df['日期'] = pd.to_datetime(df['日期'])
df = df.sort_values('日期').reset_index(drop=True)
df['close'] = df['收盘'].astype(float)

# 计算均线
df['MA5'] = df['close'].rolling(5).mean()
df['MA20'] = df['close'].rolling(20).mean()

# 金叉/死叉信号
df['signal'] = 0
df.loc[df['MA5'] > df['MA20'], 'signal'] = 1
df['position'] = df['signal'].shift(1)

# 计算收益
df['returns'] = df['close'].pct_change()
df['strategy_returns'] = df['position'] * df['returns']

cumulative = (1 + df['strategy_returns'].fillna(0)).cumprod() - 1
benchmark = (1 + df['returns'].fillna(0)).cumprod() - 1

print("均线策略回测结果")
print(f"策略累计收益: {cumulative.iloc[-1]*100:.2f}%")
print(f"基准累计收益: {benchmark.iloc[-1]*100:.2f}%")
print(f"交易次数: {(df['signal'] != df['signal'].shift(1)).sum()}")
'''

    def _generate_rsi_strategy_code(self, prompt: str) -> str:
        """生成 RSI 策略代码"""
        return '''import akshare as ak
import pandas as pd
import numpy as np

# 获取数据
df = ak.index_zh_a_hist(symbol="000300", period="daily", start_date="20230101", end_date="20241231")
df['日期'] = pd.to_datetime(df['日期'])
df = df.sort_values('日期').reset_index(drop=True)
df['close'] = df['收盘'].astype(float)

# 计算 RSI(14)
delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
rs = gain / loss
df['RSI'] = 100 - (100 / (1 + rs))

# 超买超卖信号
oversold = df['RSI'] < 30
overbought = df['RSI'] > 70

print("RSI 分析结果")
print(f"超卖次数 (RSI<30): {oversold.sum()}")
print(f"超买次数 (RSI>70): {overbought.sum()}")
print(f"当前 RSI: {df['RSI'].iloc[-1]:.1f}")

if df['RSI'].iloc[-1] < 30:
    print("信号: 超卖区域，关注反弹机会")
elif df['RSI'].iloc[-1] > 70:
    print("信号: 超买区域，注意风险")
else:
    print("信号: 正常区间")
'''

    def _generate_generic_backtest_code(self, prompt: str) -> str:
        """生成通用回测框架代码"""
        return f'''import akshare as ak
import pandas as pd

# 用户策略: {prompt[:50]}...
# 获取沪深300数据作为示例
df = ak.index_zh_a_hist(symbol="000300", period="daily", start_date="20230101", end_date="20241231")
df['日期'] = pd.to_datetime(df['日期'])
df = df.sort_values('日期').reset_index(drop=True)
df['close'] = df['收盘'].astype(float)

print("数据概览:")
print(df[['日期', '收盘', '涨跌幅']].tail(10).to_string(index=False))
print(f"\n数据范围: {{df['日期'].min().date()}} ~ {{df['日期'].max().date()}}")
print(f"总交易日: {{len(df)}}")
'''

    # ── 策略扩写（自然语言 → 结构化策略）──

    def _rule_based_expand(self, prompt: str) -> str:
        """
        基于规则的策略扩写。
        将用户大白话策略描述扩写为结构化策略文本。
        TODO: 接入 LLM（cloud_beta）进行真正的自然语言扩写。
        """
        prompt_lower = prompt.lower()

        # 识别数据源
        data_source = "沪深300指数日K线（akshare）"
        if any(k in prompt_lower for k in ["上证", "sh000001", "上证指数"]):
            data_source = "上证指数日K线（akshare）"
        elif any(k in prompt_lower for k in ["中证", "中证500", "中证1000"]):
            data_source = "中证500指数日K线（akshare）"
        elif any(k in prompt_lower for k in ["个股", "股票", "某只股票"]):
            data_source = "个股日K线（akshare，用户指定股票代码）"

        # 识别信号条件
        signal = "待明确"
        if any(k in prompt_lower for k in ["回撤", "下跌", "跌幅", "跌破"]):
            signal = "当指数在N天内回撤超过X%，触发买入信号"
        elif any(k in prompt_lower for k in ["均线", "金叉", "死叉"]):
            signal = "当短期均线上穿/下穿长期均线，触发买入/卖出信号"
        elif any(k in prompt_lower for k in ["rsi", "超买", "超卖"]):
            signal = "当RSI指标低于30/高于70，触发买入/卖出信号"
        elif any(k in prompt_lower for k in ["macd", "macd金叉"]):
            signal = "当MACD柱状线由负转正/正转负，触发买入/卖出信号"
        elif any(k in prompt_lower for k in ["放量", "缩量", "成交量"]):
            signal = "当成交量相对近期均值放大/缩小超过X%，配合价格信号触发"

        # 识别持有期
        holding_period = "30天（建议根据策略特性调整）"
        import re
        m = re.search(r'(\d+)\s*[天日]', prompt)
        if m:
            holding_period = f"{m.group(1)}天"
        elif "一个月" in prompt:
            holding_period = "30天"
        elif "三个月" in prompt:
            holding_period = "90天"
        elif "一年" in prompt:
            holding_period = "252个交易日"

        # 识别买入/卖出
        action = "买入"
        if "卖出" in prompt or "减仓" in prompt:
            action = "卖出"
        elif "买入" in prompt or "加仓" in prompt:
            action = "买入"
        elif any(k in prompt_lower for k in ["做多", "long"]):
            action = "做多（买入）"
        elif any(k in prompt_lower for k in ["做空", "short"]):
            action = "做空（卖出）"

        return f"""# 策略扩写

## 策略名称
{prompt[:20]}策略

## 数据源
{data_source}

## 核心逻辑
{signal}
- 操作方向: {action}
- 持有期: {holding_period}

## 回测要求
- 回测区间: 2019年至今（覆盖牛市、熊市、震荡市）
- 输出指标: 总交易次数、胜率、平均收益率、最大单笔收益、最大单笔亏损

## 输出要求
- result.txt: 回测统计结论
- result.csv: 每笔交易的详细记录
- chart.png: 策略净值曲线图

## 用户原始描述
{prompt}
"""

    # ── 代码校验 ──

    def _validate_code(self, code: str) -> dict:
        """基础代码校验"""
        try:
            import ast
            ast.parse(code)
            return {"valid": True, "error": ""}
        except SyntaxError as e:
            return {"valid": False, "error": f"语法错误: {e}"}
