from alerts.abstract_alert import AbstractAlert
from ta.indicators.rsi_indicator import RSIIndicator


class GreenAlert(AbstractAlert):
    def __init__(self, lookback=5, lookback_rsi=13):
        super().__init__()
        self.lookback = lookback
        self.rsi = RSIIndicator(period=lookback_rsi)

    def alert(self, ohlcv):
        data = ohlcv.copy()

        data['rsi'] =  self.rsi.rsi(data)
        data['rsi_slope'] = (
            data['rsi'] - data['rsi'].shift(self.lookback)) / self.lookback

        buy = (
            data['rsi_slope'] > 0 and
            data['rsi_slope'].shift(1) < 0 and
            data['rsi'] < 25
        )

        sell = (
            data['rsi_slope'] < 0 and
            data['rsi_slope'].shift(1) > 0 and
            data['rsi'] > 75
        )

        return buy, sell