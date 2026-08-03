import akshare as ak
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import os

plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei']
plt.rcParams['axes.unicode_minus'] = False

OUTPUT_DIR = r"D:\TRAE_SOLO_download\ve3\ve4\tactical\output"

STRATEGY_CONFIG = {
    "strategy_name": "沪深300放量买入策略",
    "index_symbol": "sh000300",
    "lookback_days": 30,
    "volume_mean_multiplier": 2.0,
    "volume_prev_day_multiplier": 1.2,
    "hold_days": 30,
    "take_profit_pct": 10.0,
    "stop_loss_pct": 5.0,
    "start_date": "20190101",
    "end_date": datetime.now().strftime("%Y%m%d"),
}


def ve4_tactical_ds_fetch_index_data(symbol, start_date, end_date):
    try:
        df = ak.stock_zh_index_daily(symbol=symbol)
        if df.empty:
            raise ValueError("获取数据为空")
        df['date'] = pd.to_datetime(df['date'])
        df = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
        df = df.sort_values('date').reset_index(drop=True)
        return df
    except Exception as e:
        print(f"获取指数数据失败: {e}")
        raise


def ve4_tactical_strat_generate_signals(df, config):
    df = df.copy()
    df['volume_ma30'] = df['volume'].rolling(window=config['lookback_days']).mean()
    df['volume_prev_day'] = df['volume'].shift(1)
    df['volume_condition1'] = df['volume'] > df['volume_ma30'] * config['volume_mean_multiplier']
    df['volume_condition2'] = df['volume'] > df['volume_prev_day'] * config['volume_prev_day_multiplier']
    df['buy_signal'] = df['volume_condition1'] & df['volume_condition2']
    return df


def ve4_tactical_strat_backtest(df, config):
    signals = ve4_tactical_strat_generate_signals(df, config)
    trade_records = []
    holding = False
    buy_date = None
    buy_price = None

    for i in range(len(signals)):
        current_date = signals.loc[i, 'date']
        current_close = signals.loc[i, 'close']

        if signals.loc[i, 'buy_signal'] and not holding:
            holding = True
            buy_date = current_date
            buy_price = current_close
            entry_idx = i

        elif holding:
            days_held = (current_date - buy_date).days
            current_return = (current_close - buy_price) / buy_price * 100

            sell_condition = (
                days_held >= config['hold_days'] or
                current_return >= config['take_profit_pct'] or
                current_return <= -config['stop_loss_pct']
            )

            if sell_condition:
                holding = False
                exit_idx = min(i, len(signals) - 1)
                sell_price = signals.loc[exit_idx, 'close']
                sell_date = signals.loc[exit_idx, 'date']
                actual_return = (sell_price - buy_price) / buy_price * 100
                actual_hold_days = (sell_date - buy_date).days

                trade_records.append({
                    'buy_date': buy_date.strftime('%Y-%m-%d'),
                    'buy_price': round(buy_price, 2),
                    'sell_date': sell_date.strftime('%Y-%m-%d'),
                    'sell_price': round(sell_price, 2),
                    'return_pct': round(actual_return, 2),
                    'hold_days': actual_hold_days,
                    'exit_reason': '止盈' if actual_return >= config['take_profit_pct'] else (
                        '止损' if actual_return <= -config['stop_loss_pct'] else '持有到期'
                    ),
                })

    if holding:
        exit_idx = len(signals) - 1
        sell_price = signals.loc[exit_idx, 'close']
        sell_date = signals.loc[exit_idx, 'date']
        actual_return = (sell_price - buy_price) / buy_price * 100
        actual_hold_days = (sell_date - buy_date).days
        trade_records.append({
            'buy_date': buy_date.strftime('%Y-%m-%d'),
            'buy_price': round(buy_price, 2),
            'sell_date': sell_date.strftime('%Y-%m-%d'),
            'sell_price': round(sell_price, 2),
            'return_pct': round(actual_return, 2),
            'hold_days': actual_hold_days,
            'exit_reason': '持有到期',
        })

    return pd.DataFrame(trade_records), signals


def ve4_tactical_strat_analyze_results(trades, config):
    if trades.empty:
        return {
            'total_trades': 0,
            'win_rate': 0,
            'avg_return': 0,
            'max_return': 0,
            'min_return': 0,
            'total_return': 0,
            'win_count': 0,
            'lose_count': 0,
        }

    total_trades = len(trades)
    win_count = len(trades[trades['return_pct'] > 0])
    lose_count = len(trades[trades['return_pct'] <= 0])
    win_rate = (win_count / total_trades) * 100
    avg_return = trades['return_pct'].mean()
    max_return = trades['return_pct'].max()
    min_return = trades['return_pct'].min()
    total_return = (1 + trades['return_pct'] / 100).prod() - 1

    return {
        'total_trades': total_trades,
        'win_count': win_count,
        'lose_count': lose_count,
        'win_rate': round(win_rate, 2),
        'avg_return': round(avg_return, 2),
        'max_return': round(max_return, 2),
        'min_return': round(min_return, 2),
        'total_return': round(total_return * 100, 2),
    }


def ve4_tactical_strat_calc_180d_return(trades, signals):
    results = []
    for _, trade in trades.iterrows():
        buy_date = pd.to_datetime(trade['buy_date'])
        target_date = buy_date + timedelta(days=180)
        buy_price = trade['buy_price']

        future_data = signals[signals['date'] >= target_date]
        if not future_data.empty:
            idx_180d = future_data.index[0]
            price_180d = signals.loc[idx_180d, 'close']
            date_180d = signals.loc[idx_180d, 'date']
            return_180d = (price_180d - buy_price) / buy_price * 100
        else:
            date_180d = signals['date'].iloc[-1]
            price_180d = signals['close'].iloc[-1]
            return_180d = (price_180d - buy_price) / buy_price * 100

        results.append({
            'buy_date': trade['buy_date'],
            'buy_price': buy_price,
            'date_180d': date_180d.strftime('%Y-%m-%d'),
            'price_180d': round(price_180d, 2),
            'return_180d_pct': round(return_180d, 2),
        })

    return pd.DataFrame(results)


def ve4_tactical_strat_save_results(trades, metrics, analysis_180d, signals, config):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    txt_path = os.path.join(OUTPUT_DIR, 'result.txt')
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write(f"策略: {config['strategy_name']}\n")
        f.write(f"数据源: akshare (沪深300指数日K线)\n")
        f.write(f"时间范围: {config['start_date']} ~ {config['end_date']}\n\n")
        f.write("策略参数:\n")
        f.write(f"  - 放量条件: 当日成交量 > 前{config['lookback_days']}天均值 × {config['volume_mean_multiplier']}\n")
        f.write(f"  - 放量条件: 当日成交量 > 前一日成交量 × {config['volume_prev_day_multiplier']}\n")
        f.write(f"  - 持有期: {config['hold_days']}天\n")
        f.write(f"  - 止盈: {config['take_profit_pct']}%\n")
        f.write(f"  - 止损: {config['stop_loss_pct']}%\n\n")
        f.write("核心指标:\n")
        f.write(f"  - 总交易次数: {metrics['total_trades']}\n")
        f.write(f"  - 盈利次数: {metrics['win_count']}\n")
        f.write(f"  - 亏损次数: {metrics['lose_count']}\n")
        f.write(f"  - 胜率: {metrics['win_rate']}%\n")
        f.write(f"  - 平均收益率: {metrics['avg_return']}%\n")
        f.write(f"  - 最大单笔收益: {metrics['max_return']}%\n")
        f.write(f"  - 最大单笔亏损: {metrics['min_return']}%\n")
        f.write(f"  - 累计收益率: {metrics['total_return']}%\n\n")

        if not analysis_180d.empty:
            f.write("180天持有分析:\n")
            f.write(f"  - 180天平均收益率: {round(analysis_180d['return_180d_pct'].mean(), 2)}%\n")
            f.write(f"  - 180天最大收益率: {round(analysis_180d['return_180d_pct'].max(), 2)}%\n")
            f.write(f"  - 180天最小收益率: {round(analysis_180d['return_180d_pct'].min(), 2)}%\n")
            f.write(f"  - 180天胜率: {round(len(analysis_180d[analysis_180d['return_180d_pct'] > 0]) / len(analysis_180d) * 100, 2)}%\n\n")

        f.write("结论: ")
        if metrics['total_trades'] > 0:
            if metrics['win_rate'] > 50:
                f.write(f"策略有效，胜率{metrics['win_rate']}%，建议进一步优化参数。\n")
            else:
                f.write(f"策略胜率{metrics['win_rate']}%，需调整参数或结合其他信号。\n")
        else:
            f.write("未产生交易信号，策略条件过于严格。\n")

    csv_path = os.path.join(OUTPUT_DIR, 'result.csv')
    trades['strategy_name'] = config['strategy_name']
    trades.to_csv(csv_path, index=False, encoding='utf-8-sig')

    fig, axes = plt.subplots(2, 1, figsize=(14, 12))

    axes[0].plot(signals['date'], signals['close'], label='沪深300收盘价', color='blue')
    buy_dates = trades['buy_date'].tolist()
    sell_dates = trades['sell_date'].tolist()
    buy_prices = trades['buy_price'].tolist()
    sell_prices = trades['sell_price'].tolist()
    for buy_date, buy_price, sell_date, sell_price in zip(buy_dates, buy_prices, sell_dates, sell_prices):
        b_date = pd.to_datetime(buy_date)
        s_date = pd.to_datetime(sell_date)
        axes[0].scatter(b_date, buy_price, color='green', marker='^', s=100, label='买入' if buy_date == buy_dates[0] else '')
        axes[0].scatter(s_date, sell_price, color='red', marker='v', s=100, label='卖出' if sell_date == sell_dates[0] else '')
        axes[0].plot([b_date, s_date], [buy_price, sell_price], color='orange', linestyle='--')
    axes[0].set_title(f"{config['strategy_name']} - 买卖信号")
    axes[0].legend()
    axes[0].grid(True)

    axes[1].plot(signals['date'], signals['volume'], label='成交量', color='gray')
    axes[1].plot(signals['date'], signals['volume_ma30'], label='MA30成交量', color='orange')
    axes[1].fill_between(signals['date'], signals['volume'], signals['volume_ma30'] * config['volume_mean_multiplier'],
                         where=signals['buy_signal'], color='green', alpha=0.3, label='买入信号区')
    axes[1].set_title('成交量与放量信号')
    axes[1].legend()
    axes[1].grid(True)

    plt.tight_layout()
    chart_path = os.path.join(OUTPUT_DIR, 'chart.png')
    plt.savefig(chart_path, dpi=100, bbox_inches='tight')
    plt.close()

    print(f"结果已保存到: {OUTPUT_DIR}")
    print(f"- result.txt")
    print(f"- result.csv")
    print(f"- chart.png")


def main():
    print("=" * 60)
    print(f"{STRATEGY_CONFIG['strategy_name']}")
    print("=" * 60)

    df = ve4_tactical_ds_fetch_index_data(
        STRATEGY_CONFIG['index_symbol'],
        STRATEGY_CONFIG['start_date'],
        STRATEGY_CONFIG['end_date']
    )
    print(f"数据获取完成: {len(df)} 条记录")
    print(f"时间范围: {df['date'].min().strftime('%Y-%m-%d')} ~ {df['date'].max().strftime('%Y-%m-%d')}")

    trades, signals = ve4_tactical_strat_backtest(df, STRATEGY_CONFIG)
    print(f"\n回测完成: 共产生 {len(trades)} 笔交易")

    metrics = ve4_tactical_strat_analyze_results(trades, STRATEGY_CONFIG)
    print(f"\n核心指标:")
    print(f"  - 总交易次数: {metrics['total_trades']}")
    print(f"  - 胜率: {metrics['win_rate']}%")
    print(f"  - 平均收益率: {metrics['avg_return']}%")
    print(f"  - 最大单笔收益: {metrics['max_return']}%")
    print(f"  - 最大单笔亏损: {metrics['min_return']}%")

    analysis_180d = ve4_tactical_strat_calc_180d_return(trades, signals)
    if not analysis_180d.empty:
        print(f"\n180天持有分析:")
        print(f"  - 180天平均收益率: {round(analysis_180d['return_180d_pct'].mean(), 2)}%")
        print(f"  - 180天胜率: {round(len(analysis_180d[analysis_180d['return_180d_pct'] > 0]) / len(analysis_180d) * 100, 2)}%")

    ve4_tactical_strat_save_results(trades, metrics, analysis_180d, signals, STRATEGY_CONFIG)


if __name__ == "__main__":
    main()