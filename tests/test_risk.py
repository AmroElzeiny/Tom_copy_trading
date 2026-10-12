from tg_mt5_copytrader.risk import floor_volume_to_step, partial_close_volume, risk_sized_volume


def test_volume_floor():
    assert floor_volume_to_step(0.126, 0.01, 10.0, 0.01) == 0.12


def test_one_percent_risk():
    # Equity 10,000; 1% = 100 risk. One lot would lose 500 to SL => 0.2 lots.
    v = risk_sized_volume(
        equity=10_000,
        risk_pct=1.0,
        loss_per_one_lot=500,
        min_volume=0.01,
        max_volume=10,
        step=0.01,
        hard_max_volume=10,
    )
    assert v == 0.2


def test_partial_close_50_percent():
    assert partial_close_volume(current_volume=0.4, fraction=0.5, min_volume=0.01, step=0.01) == 0.2


def test_partial_close_does_not_leave_tiny_residual():
    # 0.03 * 75% floors to 0.02 and leaves 0.01, valid.
    assert partial_close_volume(current_volume=0.03, fraction=0.75, min_volume=0.01, step=0.01) == 0.02
