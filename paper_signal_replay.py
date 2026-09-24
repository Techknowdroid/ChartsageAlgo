"""Long-only replay of recorded historical signals; never evaluates rules."""
import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class PositionSize:
    """Auditable result of sizing one BUY against a fixed capital target."""

    quantity: int
    actual_investment: float
    allocation_percent: float
    tolerance_increased_quantity: bool


class PositionSizer:
    """Size integer-share BUYs around one fixed reference-capital amount.

    Nearest-integer sizing keeps each purchase close to the target.  The
    minimum-allocation floor prevents a rounded position from investing less
    than the configured share of that target.  At least one share is always
    purchased, including when its price exceeds the reference capital.
    """

    def __init__(self, reference_capital, minimum_allocation=0.90):
        self.reference_capital = float(reference_capital)
        self.minimum_allocation = float(minimum_allocation)
        if not math.isfinite(self.reference_capital) or self.reference_capital <= 0:
            raise ValueError('Reference capital must be a positive finite amount.')
        if (
            not math.isfinite(self.minimum_allocation)
            or not 0 < self.minimum_allocation <= 1
        ):
            raise ValueError('Minimum allocation must be greater than 0 and at most 1.')

    def size(self, entry_price):
        price = float(entry_price)
        if not math.isfinite(price) or price <= 0:
            raise ValueError('Entry price must be a positive finite amount.')

        ratio = self.reference_capital / price
        rounded_quantity = max(1, int(ratio + 0.5))
        minimum_quantity = max(
            1,
            math.ceil(self.reference_capital * self.minimum_allocation / price),
        )
        quantity = max(rounded_quantity, minimum_quantity)
        actual = quantity * price
        return PositionSize(
            quantity=quantity,
            actual_investment=actual,
            allocation_percent=actual / self.reference_capital * 100.0,
            tolerance_increased_quantity=quantity > rounded_quantity,
        )


_POSITION_PNL_FIELDS = {'Profit %', 'Loss %', 'Stop Loss %', 'P&L %'}
_POSITION_OPERATORS = {'>', '>=', '<', '<=', '='}


def _active_position_rule(node):
    if not isinstance(node, dict) or not bool(node.get('enabled', True)):
        return False
    if node.get('type') == 'rule':
        return True
    return any(_active_position_rule(child) for child in node.get('children', []))


def validate_position_exit_expression(node):
    """Accept boolean expressions made only from holding P&L comparisons."""
    if not isinstance(node, dict) or not bool(node.get('enabled', True)):
        raise ValueError('The Paper Backtest profit EXIT expression is disabled or empty.')
    if node.get('type') == 'group':
        children = [
            child for child in node.get('children', [])
            if _active_position_rule(child)
        ]
        if not children:
            raise ValueError('The Paper Backtest profit EXIT expression has no active rule.')
        for child in children:
            validate_position_exit_expression(child)
        return
    if node.get('type') != 'rule':
        raise ValueError('Invalid Paper Backtest profit EXIT expression.')
    left, right = node.get('left', {}), node.get('right', {})
    operands = (left, right)
    fields = [
        operand for operand in operands
        if operand.get('kind') == 'field'
    ]
    values = [
        operand for operand in operands
        if operand.get('kind') == 'value'
    ]
    if (
        len(fields) != 1 or len(values) != 1
        or fields[0].get('name') not in _POSITION_PNL_FIELDS
        or node.get('operator') not in _POSITION_OPERATORS
    ):
        raise ValueError(
            'Paper Backtest position EXIT templates must compare Profit %, '
            'Loss %, or Stop Loss % with a numeric value using >, >=, <, <=, or =.'
        )
    try:
        threshold = float(values[0].get('value'))
    except (TypeError, ValueError) as exc:
        raise ValueError('The Paper Backtest profit EXIT threshold is invalid.') from exc
    if not math.isfinite(threshold):
        raise ValueError('The Paper Backtest profit EXIT threshold is invalid.')


def evaluate_position_exit_expression(node, profit_percent):
    """Return (matched, evidence) for one simulated holding on one date."""
    if node.get('type') == 'group':
        outcomes, evidence = [], []
        for child in node.get('children', []):
            if not _active_position_rule(child):
                continue
            matched, child_evidence = evaluate_position_exit_expression(
                child, profit_percent
            )
            outcomes.append(matched)
            evidence.extend(child_evidence)
        op = str(node.get('op', 'AND')).upper()
        if op == 'OR':
            return any(outcomes), evidence
        if op.startswith('ANY '):
            try:
                required = max(1, int(op.split()[1]))
            except (IndexError, ValueError):
                required = 1
            return sum(bool(value) for value in outcomes) >= required, evidence
        return all(outcomes), evidence

    left, right = node['left'], node['right']

    def operand_value(operand):
        if operand.get('kind') != 'field':
            return float(operand.get('value'))
        if operand.get('name') == 'Loss %':
            return max(0.0, -float(profit_percent))
        return float(profit_percent)

    left_value = operand_value(left)
    right_value = operand_value(right)
    operator = node['operator']
    matched = {
        '>': left_value > right_value,
        '>=': left_value >= right_value,
        '<': left_value < right_value,
        '<=': left_value <= right_value,
        '=': math.isclose(left_value, right_value, rel_tol=1e-9, abs_tol=1e-9),
    }[operator]
    field_name = (
        left.get('name') if left.get('kind') == 'field' else right.get('name')
    )
    field_value = left_value if left.get('kind') == 'field' else right_value
    threshold = right_value if left.get('kind') == 'field' else left_value
    return matched, [
        f'{field_name} {operator} {threshold:g}% '
        f'[{field_value:.4f}% vs {threshold:g}%]'
    ]


def xirr(contributions, end, terminal):
    """Annualized return for external contributions and a terminal valuation."""
    flows = {}
    for day, amount in contributions:
        flows[day] = flows.get(day, 0.0) - amount
    flows[end] = flows.get(end, 0.0) + terminal
    flows = {day: amount for day, amount in flows.items() if abs(amount) > 1e-9}
    if not flows or len(flows) < 2:
        return None
    origin = date.fromisoformat(min(flows))
    terms = [((date.fromisoformat(day)-origin).days/365.0, amount) for day, amount in flows.items()]
    if not any(v > 0 for _, v in terms) or not any(v < 0 for _, v in terms):
        return None
    def npv(log_rate):
        exponents = [-log_rate*t for t, _ in terms]
        scale = max(exponents)
        return sum(v*math.exp(e-scale) for (_, v), e in zip(terms, exponents))
    lo, hi = -700.0, 700.0
    if npv(lo)*npv(hi) > 0:
        return None
    for _ in range(240):
        mid = (lo+hi)/2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    result = math.expm1((lo+hi)/2)*100
    return result if math.isfinite(result) else None


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _campaign_statistics(campaigns):
    closed = [row for row in campaigns if row['Status'] == 'CLOSED']
    open_rows = [row for row in campaigns if row['Status'] == 'OPEN']
    returns = [float(row['ReturnPercent']) for row in closed]
    winners = [value for value in returns if value > 0]
    losers = [value for value in returns if value < 0]
    profits = [float(row['ProfitLoss']) for row in closed if row['ProfitLoss'] > 0]
    losses = [-float(row['ProfitLoss']) for row in closed if row['ProfitLoss'] < 0]
    average = lambda values: sum(values) / len(values) if values else None
    average_winner, average_loser = average(winners), average(losers)
    win_rate = len(winners) / len(closed) * 100.0 if closed else None
    expectancy = (
        average(returns) if returns else None
    )
    return dict(
        ClosedCampaigns=len(closed),
        OpenCampaigns=len(open_rows),
        WinningCampaigns=len(winners),
        LosingCampaigns=len(losers),
        WinRatePercent=win_rate,
        EqualWeightedAverageCampaignReturnPercent=average(returns),
        MedianCampaignReturnPercent=_median(returns),
        AverageWinnerPercent=average_winner,
        AverageLoserPercent=average_loser,
        WinnerLoserSpreadPercent=(
            average_winner + average_loser
            if average_winner is not None and average_loser is not None else None
        ),
        PayoffRatio=(
            average_winner / abs(average_loser)
            if average_winner is not None and average_loser not in (None, 0) else None
        ),
        ExpectancyPercent=expectancy,
        ProfitFactor=(sum(profits) / sum(losses) if losses else None),
        BestCampaignPercent=max(returns) if returns else None,
        WorstCampaignPercent=min(returns) if returns else None,
        AverageHoldingDays=(
            average([float(row['HoldingDays']) for row in closed]) if closed else None
        ),
    )


def _benchmark_statistics(values, contributions, end, benchmark_name):
    """Compare with fractional TRI units bought on identical contribution dates."""
    ordered = sorted((day, float(value)) for day, value in (values or {}).items()
                     if day <= end and float(value) > 0)
    if not ordered or not contributions:
        return {}, []
    first_flow = min(day for day, amount in contributions if amount > 0)
    relevant = [(day, value) for day, value in ordered if first_flow <= day <= end]
    if not relevant:
        return {}, []

    ordered_days = [day for day, _ in ordered]
    ordered_values = [value for _, value in ordered]

    def observation_on_or_before(day):
        index = bisect_right(ordered_days, day) - 1
        return (ordered_days[index], ordered_values[index]) if index >= 0 else (None, None)

    units = 0.0
    usable_flows = []
    for flow_day, amount in contributions:
        _, tri = observation_on_or_before(flow_day)
        if tri is None:
            return {}, []
        units += amount / tri
        usable_flows.append((flow_day, amount))
    end_day, end_tri = relevant[-1]
    start_observation_day, start_tri = observation_on_or_before(first_flow)
    terminal = units * end_tri
    benchmark_xirr = xirr(usable_flows, end, terminal)
    peak = 0.0
    rows = []
    for day, tri in relevant:
        normalized = tri / start_tri * 100.0
        peak = max(peak, normalized)
        drawdown = (normalized / peak - 1.0) * 100.0 if peak else 0.0
        rows.append(dict(Benchmark=benchmark_name, Date=day, TRIValue=tri, NormalizedIndex=normalized,
                         DrawdownPercent=drawdown))
    return dict(
        BenchmarkName=benchmark_name,
        BenchmarkStartDate=first_flow,
        BenchmarkStartObservationDate=start_observation_day,
        BenchmarkEndObservationDate=end_day,
        BenchmarkStartValue=start_tri,
        BenchmarkEndValue=end_tri,
        BenchmarkPeriodReturnPercent=(end_tri / start_tri - 1.0) * 100.0,
        BenchmarkXIRRPercent=benchmark_xirr,
        BenchmarkTerminalValue=terminal,
        BenchmarkMaximumDrawdownPercent=min(row['DrawdownPercent'] for row in rows),
    ), rows


def replay(
    rows_by_date,
    start,
    end,
    reference_capital=100000.0,
    cost_percent=0.0,
    valuations=None,
    holding_exit_rows_by_date=None,
    minimum_allocation=0.90,
    position_exit_rule=None,
    daily_prices=None,
    benchmark_prices=None,
    benchmark_name='NIFTY 500 TRI',
):
    if start > end or cost_percent < 0:
        raise ValueError('Invalid dates or costs.')
    position_sizer = PositionSizer(reference_capital, minimum_allocation)
    if position_exit_rule:
        validate_position_exit_expression(position_exit_rule.get('expression'))
    ledgers, holdings, summaries, reviews = [], [], [], []
    campaigns, equity_curve, benchmark_curve = [], [], []
    exit_audit = []
    # Paper replay deliberately starts without inherited holdings. Retain the
    # latest recorded BUY before the selected window so an unmatched EXIT can
    # explain that boundary explicitly instead of reporting a vague failure.
    pre_start_buy_dates = {}
    for recorded_day, recorded_rows in rows_by_date.items():
        if recorded_day >= start:
            continue
        for recorded_row in recorded_rows:
            if str(recorded_row.get('Signal', '')) == 'BUY':
                symbol = str(recorded_row.get('Symbol', ''))
                if symbol and recorded_day > pre_start_buy_dates.get(symbol, ''):
                    pre_start_buy_dates[symbol] = recorded_day
    exit_source = holding_exit_rows_by_date
    if exit_source is None:
        exit_source = {
            day: [
                row for row in rows
                if str(row.get('Signal', '')).startswith('EXIT')
            ]
            for day, rows in rows_by_date.items()
        }
    for day in sorted(exit_source):
        if not start <= day <= end:
            continue
        for row in exit_source[day]:
            exit_audit.append(dict(
                Date=day,
                Symbol=str(row.get('Symbol', '')),
                Close=row.get('Close'),
                Confirmations=row.get('Confirmation Count', ''),
                MatchedMembers=row.get('Matched Members', ''),
                Evidence=row.get('Evidence', ''),
                Executed='No',
                ExitPnLPercent='',
            ))
    for mode in ('Reference capital',):
        cash = contributed = costs = realized = 0.0
        positions = {}
        last_sell_dates = {}
        contributions = []
        event_days = set(rows_by_date)
        if holding_exit_rows_by_date is not None:
            event_days.update(holding_exit_rows_by_date)
        for prices in (daily_prices or {}).values():
            event_days.update(prices)
        previous_equity = 0.0
        twr_multiplier = 1.0
        twr_peak = 1.0
        for day in sorted(event_days):
            if not start <= day <= end:
                continue
            # Mark existing holdings before processing that session's signals.
            for symbol, pos in positions.items():
                raw_mark = (daily_prices or {}).get(symbol, {}).get(day)
                try:
                    mark_price = float(raw_mark)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(mark_price) and mark_price > 0:
                    pos['price'], pos['date'] = mark_price, day
            day_contribution = 0.0
            # Exits release cash before purchases on the same date.
            normal_rows = list(rows_by_date.get(day, []))
            position_exit_rows = []
            if position_exit_rule:
                for symbol, pos in list(positions.items()):
                    raw_price = (daily_prices or {}).get(symbol, {}).get(day)
                    try:
                        price = float(raw_price)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(price) or price <= 0 or pos['quantity'] <= 0:
                        continue
                    average_cost = pos['basis'] / pos['quantity']
                    profit_percent = (price - average_cost) / average_cost * 100.0
                    matched, evidence = evaluate_position_exit_expression(
                        position_exit_rule['expression'], profit_percent
                    )
                    if matched:
                        position_exit_rows.append(dict(
                            Symbol=symbol,
                            Signal='EXIT / PROFIT TARGET',
                            Close=price,
                            **{
                                'Matched Members': position_exit_rule.get('name', ''),
                                'Evidence': ' | '.join(evidence),
                                'Average Cost': average_cost,
                                'Profit % at Exit': profit_percent,
                                '_paper_position_exit': True,
                            },
                        ))
            if holding_exit_rows_by_date is None:
                rows = position_exit_rows + normal_rows
            else:
                rows = position_exit_rows + [
                    row for row in normal_rows
                    if not str(row.get('Signal', '')).startswith('EXIT')
                ] + list(holding_exit_rows_by_date.get(day, []))
            rows = sorted(rows, key=lambda r: (not str(r.get('Signal', '')).startswith('EXIT'), str(r.get('Symbol', ''))))
            buy_symbols_today = {
                str(row.get('Symbol', ''))
                for row in rows
                if str(row.get('Signal', '')) == 'BUY'
            }
            seen = set()
            for row in rows:
                symbol, signal = str(row.get('Symbol', '')), str(row.get('Signal', ''))
                signal_key = 'EXIT' if signal.startswith('EXIT') else signal
                if (symbol, signal_key) in seen:
                    continue
                seen.add((symbol, signal_key))
                try:
                    price = float(row.get('Close'))
                    if not math.isfinite(price) or price <= 0:
                        raise ValueError()
                except (TypeError, ValueError):
                    reviews.append(dict(Mode=mode, Date=day, Symbol=symbol, Reason='Missing/invalid recorded price'))
                    continue
                if symbol in positions:
                    positions[symbol]['price'], positions[symbol]['date'] = price, day
                if signal != 'BUY' and not signal.startswith('EXIT'):
                    reviews.append(dict(Mode=mode, Date=day, Symbol=symbol, Reason='Ambiguous signal: ' + signal))
                    continue
                topup = pnl = 0.0
                if signal == 'BUY':
                    sizing = position_sizer.size(price)
                    quantity = sizing.quantity
                    gross = quantity * price
                    fee = gross * cost_percent / 100
                    topup = max(0, gross + fee - cash)
                    contributed += topup
                    if topup > 0:
                        contributions.append((day, topup))
                        day_contribution += topup
                    cash += topup - gross - fee
                    pos = positions.setdefault(symbol, dict(
                        quantity=0.0, basis=0.0, entry_date=day, buy_count=0,
                    ))
                    pos.update(price=price, date=day)
                    pos['quantity'] += quantity
                    pos['basis'] += gross + fee
                    pos['buy_count'] += 1
                    action = 'BUY'
                else:
                    pos = positions.pop(symbol, None)
                    if pos is None:
                        if symbol in buy_symbols_today:
                            if symbol in last_sell_dates:
                                reason = (
                                    f"Already fully sold on {last_sell_dates[symbol]}; "
                                    "the same-day BUY is processed after EXIT"
                                )
                            else:
                                reason = (
                                    "No opening holding; the same-day BUY is "
                                    "processed after EXIT"
                                )
                        elif symbol in last_sell_dates:
                            reason = (
                                f"Already fully sold on {last_sell_dates[symbol]}; "
                                "no subsequent BUY"
                            )
                        elif symbol in pre_start_buy_dates:
                            reason = (
                                "Replay started with an empty portfolio; recorded "
                                f"BUY on {pre_start_buy_dates[symbol]} was before "
                                "the selected start date"
                            )
                        else:
                            reason = "No BUY was executed since the replay start date"
                        reviews.append(
                            dict(Mode=mode, Date=day, Symbol=symbol, Reason=reason)
                        )
                        continue
                    quantity = pos['quantity']
                    gross = quantity * price
                    fee = gross * cost_percent / 100
                    cash += gross - fee
                    pnl = gross - fee - pos['basis']
                    realized += pnl
                    average_cost = (
                        pos['basis'] / quantity if quantity > 0 else None
                    )
                    exit_pnl_percent = (
                        pnl / pos['basis'] * 100.0 if pos['basis'] > 0 else None
                    )
                    campaigns.append(dict(
                        Mode=mode, Symbol=symbol, Status='CLOSED',
                        EntryDate=pos['entry_date'], ExitOrValuationDate=day,
                        BuyCount=pos['buy_count'], Quantity=quantity,
                        CostBasis=pos['basis'], ExitOrMarketValue=gross-fee,
                        ProfitLoss=pnl, ReturnPercent=exit_pnl_percent,
                        HoldingDays=(date.fromisoformat(day)-date.fromisoformat(pos['entry_date'])).days,
                    ))
                    last_sell_dates[symbol] = day
                    action = 'SELL'
                    if row.get('_paper_position_exit'):
                        exit_audit.append(dict(
                            Date=day,
                            Symbol=symbol,
                            Close=price,
                            Confirmations=1,
                            MatchedMembers=row.get('Matched Members', ''),
                            Evidence=row.get('Evidence', ''),
                            Executed='Yes',
                            ExitPnLPercent=exit_pnl_percent,
                        ))
                    else:
                        # Technical EXIT matches are recorded in the audit
                        # before replay. Mark the exact row that became a sale
                        # and attach its realized return for truthful colouring.
                        audit_row = next((
                            item for item in reversed(exit_audit)
                            if item.get('Date') == day
                            and item.get('Symbol') == symbol
                            and item.get('Executed') != 'Yes'
                        ), None)
                        if audit_row is not None:
                            audit_row['Executed'] = 'Yes'
                            audit_row['ExitPnLPercent'] = exit_pnl_percent
                costs += fee
                ledgers.append(dict(Mode=mode, Date=day, Symbol=symbol, Action=action,
                    Quantity=quantity, Price=price, Gross=gross, Costs=fee,
                    Contribution=topup, Cash=cash, RealizedPnL=pnl,
                    ReferenceCapital=(
                        position_sizer.reference_capital if action == 'BUY' else ''
                    ),
                    ActualAllocationPercent=(
                        sizing.allocation_percent if action == 'BUY' else ''
                    ),
                    ToleranceRaisedQuantity=(
                        'Yes' if action == 'BUY' and sizing.tolerance_increased_quantity
                        else 'No' if action == 'BUY' else ''
                    ),
                    SignalSource=(
                        'Completed Step 1 to Step 3 BUY result'
                        if action == 'BUY'
                        else 'Paper holding profit/stop EXIT rule match'
                        if row.get('_paper_position_exit')
                        else 'Paper holding EXIT rule match'
                    ),
                    AverageCostAtExit=(
                        average_cost if action == 'SELL' else ''
                    ),
                    ProfitPercentAtExit=(
                        exit_pnl_percent if action == 'SELL' else ''
                    ),
                    MatchedMembers=row.get('Matched Members', ''),
                    Evidence=row.get('Evidence', '')))
            marked_value = sum(
                pos['quantity'] * pos['price'] for pos in positions.values()
                if pos.get('price')
            )
            ending_equity = cash + marked_value
            if previous_equity > 0:
                daily_return = (ending_equity - day_contribution) / previous_equity - 1.0
            elif day_contribution > 0:
                daily_return = ending_equity / day_contribution - 1.0
            else:
                daily_return = 0.0
            twr_multiplier *= 1.0 + daily_return
            twr_peak = max(twr_peak, twr_multiplier)
            drawdown = (twr_multiplier / twr_peak - 1.0) * 100.0
            equity_curve.append(dict(
                Date=day, PortfolioValue=ending_equity,
                ExternalContribution=day_contribution,
                DailyReturnPercent=daily_return * 100.0,
                TWRIndex=twr_multiplier * 100.0,
                DrawdownPercent=drawdown,
            ))
            previous_equity = ending_equity
        value = basis = 0.0
        missing = 0
        for symbol, pos in positions.items():
            mark = (valuations or {}).get(symbol, {})
            price, price_date = mark.get('price'), mark.get('date')
            valid = isinstance(price, (int, float)) and math.isfinite(price) and price > 0
            valid = valid and bool(price_date) and pos['date'] <= price_date <= end
            market = pos['quantity'] * price if valid else None
            if valid:
                value += market
            else:
                missing += 1
                reviews.append(dict(Mode=mode, Date=end, Symbol=symbol, Reason='End valuation unavailable: ' + str(mark.get('error', 'no valid dated closing price'))))
            basis += pos['basis']
            holdings.append(dict(Mode=mode, Symbol=symbol, Quantity=pos['quantity'],
                CostBasis=pos['basis'], ValuationPrice=price if valid else None, PriceDate=price_date if valid else '',
                PriceSource=mark.get('source', ''), MarketValue=market,
                UnrealizedPnL=market-pos['basis'] if valid else None))
            if valid:
                open_pnl = market - pos['basis']
                campaigns.append(dict(
                    Mode=mode, Symbol=symbol, Status='OPEN',
                    EntryDate=pos['entry_date'], ExitOrValuationDate=price_date,
                    BuyCount=pos['buy_count'], Quantity=pos['quantity'],
                    CostBasis=pos['basis'], ExitOrMarketValue=market,
                    ProfitLoss=open_pnl,
                    ReturnPercent=open_pnl / pos['basis'] * 100.0,
                    HoldingDays=(date.fromisoformat(price_date)-date.fromisoformat(pos['entry_date'])).days,
                ))
        annual = xirr(contributions, end, cash+value) if not missing else None
        simple_return = (
            (cash + value - contributed) / contributed * 100.0
            if contributed > 0 and not missing else None
        )
        twr = (twr_multiplier - 1.0) * 100.0 if contributions and not missing else None
        maximum_drawdown = (
            min(row['DrawdownPercent'] for row in equity_curve) if equity_curve else None
        )
        campaign_stats = _campaign_statistics(campaigns)
        benchmark_stats, benchmark_curve = _benchmark_statistics(
            benchmark_prices, contributions, end, benchmark_name
        )
        benchmark_stats['BenchmarkStatus'] = (
            'Calculated from local official TRI cache'
            if benchmark_curve else
            f'Unavailable - update the local {benchmark_name} cache'
        )
        total_pnl = cash + value - contributed if not missing else None
        if missing:
            historical_outcome = 'INCOMPLETE - missing valuation prices'
        elif contributed <= 0:
            historical_outcome = 'NO TRADES'
        elif total_pnl > 1e-9:
            historical_outcome = 'PROFITABLE AFTER COSTS'
        elif total_pnl < -1e-9:
            historical_outcome = 'UNPROFITABLE AFTER COSTS'
        else:
            historical_outcome = 'BREAK-EVEN AFTER COSTS'
        summaries.append(dict(Mode=mode,
            ReferenceCapital=position_sizer.reference_capital,
            MinimumAllocationPercent=position_sizer.minimum_allocation * 100.0,
            Contributions=contributed, Cash=cash,
            HoldingsValue=value if not missing else None, Costs=costs, RealizedPnL=realized,
            UnrealizedPnL=value-basis if not missing else None,
            FinalPortfolioValue=cash+value if not missing else None,
            TotalPnL=total_pnl,
            SimpleReturnOnContributedCapitalPercent=simple_return,
            TimeWeightedReturnPercent=twr,
            MaximumDrawdownPercent=maximum_drawdown,
            XIRRPercent=annual,
            HistoricalOutcome=historical_outcome,
            **campaign_stats,
            **benchmark_stats,
            ExcessXIRRPercent=(
                annual - benchmark_stats['BenchmarkXIRRPercent']
                if annual is not None and benchmark_stats.get('BenchmarkXIRRPercent') is not None else None
            ),
            ExcessTWRPercent=(
                twr - benchmark_stats['BenchmarkPeriodReturnPercent']
                if twr is not None and benchmark_stats.get('BenchmarkPeriodReturnPercent') is not None else None
            ),
            ReturnStatus='Missing valuation prices' if missing else 'Calculated' if annual is not None else 'XIRR undefined (no dated positive/negative cash-flow pair)',
            MissingValuations=missing))
    return dict(
        Summary=summaries,
        Transactions=ledgers,
        OpenHoldings=holdings,
        Campaigns=campaigns,
        **{'Equity Curve': equity_curve},
        Benchmark=benchmark_curve,
        Exceptions=reviews,
        **{'EXIT Evaluation Audit': exit_audit},
    )
