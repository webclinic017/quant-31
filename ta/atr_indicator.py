import pandas as pd

class ATRIndicator:
    def __init__(self, period=14):
        self.period = period

    def atr(self, data, smoothing='rma'):
        data = data.copy()
        data['previous_close'] = data['close'].shift(1)

        true_range = pd.DataFrame()
        true_range['high_low'] = data['high'] - data['low']
        true_range['high_previous_close'] = abs(data['high'] - data['previous_close'])
        true_range['low_previous_close'] = abs(data['low'] - data['previous_close'])

        data['true_range'] = true_range.max(axis=1)
        
        if smoothing == 'rma':
            data['atr'] = data['true_range'].ewm(span=self.period, adjust=False).mean()
        elif smoothing == 'wilder':
            data['atr'] = 0.0
            
            data.at[self.period, 'atr'] = data['true_range'][1:self.period + 1].mean()
            for i in range(self.period + 1, len(data)):
                data.at[i, 'atr'] = (data.at[i - 1, 'atr'] * (self.period - 1) + data.at[i, 'true_range']) / self.period
        else:
            data['atr'] = data['true_range'].rolling(window=self.period).mean()
        
        return data['atr']