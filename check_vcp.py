import pandas as pd
from strategies import resample, roostoo_candidates, liquid
from flow_backtest import hist, run_vcp, portfolio

IV, BPD, L, MC, GATE = "4h", 6, 6, 1, False

frames = {}
for c in roostoo_candidates():
    d = hist(c)
    if d is not None and liquid(d):
        frames[c] = resample(d, IV)

allt = [(c, *x) for c, df in frames.items() for x in run_vcp(df, BPD, L, MC, GATE)]
t = pd.DataFrame(allt, columns=["coin", "t_in", "t_out", "ret"]).sort_values("t_in")
t["net"] = t.ret - 0.002


def tuples(df):
    return list(df[["coin", "t_in", "t_out", "ret"]].itertuples(index=False, name=None))


print("FULL:", portfolio(allt))
mid = t.t_in.iloc[len(t) // 2]
print("H1  :", portfolio(tuples(t[t.t_in < mid])))
print("H2  :", portfolio(tuples(t[t.t_in >= mid])))

print("\nBy month:")
print(t.groupby(t.t_in.dt.to_period("M")).net.agg(["count", "sum", "mean"]).round(4))

by = t.groupby("coin").net.agg(["count", "sum"]).sort_values("sum", ascending=False).round(4)
print("\nBest coins:\n", by.head(8), "\nWorst coins:\n", by.tail(5))

top10 = t.net.nlargest(10).sum()
print(f"\nTotal net sum: {t.net.sum():.3f} | top 10 trades: {top10:.3f} | "
      f"without top 10: {t.net.sum() - top10:.3f}")