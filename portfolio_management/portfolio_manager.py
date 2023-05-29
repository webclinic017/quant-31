import asyncio
from enum import Enum, auto
from typing import Dict, Type, Union
from analytics.abstract_analytics import AbstractAnalytics
from core.event_dispatcher import register_handler
from core.events.ohlcv import OHLCVEvent
from core.events.portfolio import PortfolioPerformanceEvent
from core.events.position import PositionClosed, OrderFilled, LongPositionOpened, PositionReadyToClose, ShortPositionOpened, PositionSide
from core.events.risk import RiskEvaluate, RiskExit
from core.events.strategy import LongExit, ShortExit, LongGo, ShortGo
from core.position import Position
from datasource.abstract_datasource import AbstractDatasource
from .position_sizer import PositionSizer
from .position_storage import PositionStorage
from .abstract_portfolio_manager import AbstractPortfolioManager


class PositionState(Enum):
    IDLE = auto()
    OPENING = auto()
    OPENED = auto()
    CLOSING = auto()


PortfolioEvent = Union[LongGo, ShortGo, LongExit, ShortExit, RiskExit, OrderFilled, PositionClosed, OHLCVEvent]


class PortfolioManager(AbstractPortfolioManager):
    def __init__(self, datasource: Type[AbstractDatasource], analytics: Type[AbstractAnalytics], leverage: int = 1, risk_per_trade: float = 0.001):
        super().__init__()
        self.datasource = datasource
        self.analytics = analytics
        self.risk_per_trade = risk_per_trade
        self.leverage = leverage

        self.position_storage = PositionStorage()
        self.state: Dict[str, PositionState] = {}
        self.state_lock = asyncio.Lock()

        self._state_handlers = {
            (PositionState.IDLE, LongGo): self.handle_open_position,
            (PositionState.IDLE, ShortGo): self.handle_open_position,
            (PositionState.OPENING, OrderFilled): self.handle_order_filled,
            (PositionState.OPENED, OHLCVEvent): self.handle_market,
            (PositionState.OPENED, LongExit): self.handle_exit,
            (PositionState.OPENED, ShortExit): self.handle_exit,
            (PositionState.OPENED, RiskExit): self.handle_exit,
            (PositionState.CLOSING, PositionClosed): self.handle_closed_position,
        }

    @register_handler(OHLCVEvent)
    async def _on_market(self, event: OHLCVEvent):
        await self.process_event(event)

    @register_handler(LongGo)
    async def _on_go_long(self, event: LongGo):
        await self.process_event(event)

    @register_handler(ShortGo)
    async def _on_go_short(self, event: ShortGo):
        await self.process_event(event)

    @register_handler(LongExit)
    async def _on_exit_long(self, event: LongExit):
        await self.process_event(event)

    @register_handler(ShortExit)
    async def _on_exit_short(self, event: ShortExit):
        await self.process_event(event)

    @register_handler(RiskExit)
    async def _on_exit_risk(self, event: RiskExit):
        await self.process_event(event)

    @register_handler(OrderFilled)
    async def _on_order_filled(self, event: OrderFilled):
        await self.process_event(event)

    @register_handler(PositionClosed)
    async def _on_closed_position(self, event: PositionClosed):
        await self.process_event(event)

    async def handle_open_position(self, event: Union[LongGo, ShortGo]) -> bool:
        active_position = await self.position_storage.get_active_position(event.symbol)

        if active_position is not None:
            return False

        position = await self.create_position(event)

        await self.position_storage.add_active_position(event.symbol, position)

        open_position_event = self.create_open_position_event(position)

        if not open_position_event:
            return False

        await self.dispatcher.dispatch(open_position_event)

        return True

    async def handle_order_filled(self, event: OrderFilled) -> bool:
        active_position = await self.position_storage.get_active_position(event.symbol)

        if active_position is None:
            return False

        active_position.add_order(event.order)
        active_position.update_prices(event.order.price)

        await self.position_storage.add_active_position(event.symbol, active_position)

        return True

    async def handle_closed_position(self, event: PositionClosed) -> bool:
        active_position = await self.position_storage.get_active_position(event.symbol)

        if active_position is None:
            return False

        await self.close_position(active_position, event.exit_price)
        await self.update_position_performance(active_position)

        return True

    async def handle_market(self, event: OHLCVEvent) -> bool:
        active_position = await self.position_storage.get_active_position(event.symbol)

        if active_position is None:
            return False

        await self.dispatcher.dispatch(
            RiskEvaluate(
                symbol=active_position.symbol,
                timeframe=active_position.timeframe,
                side=active_position.side,
                size=active_position.size,
                entry=active_position.entry_price,
                stop_loss=active_position.stop_loss_price,
                risk_reward_ratio=active_position.risk_reward_ratio,
                strategy=active_position.strategy,
                risk_per_trade=self.risk_per_trade,
                ohlcv=event.ohlcv
            )
        )

        return True

    async def handle_exit(self, event: Union[LongExit, ShortExit, RiskExit]) -> bool:
        active_position = await self.position_storage.get_active_position(event.symbol)

        if active_position is None or (event.strategy != active_position.strategy) or not self.can_close_position(active_position.side, active_position.entry_price, event):
            return False

        await self.dispatcher.dispatch(
            PositionReadyToClose(
                symbol=event.symbol,
                timeframe=event.timeframe, exit_price=event.exit
            )
        )

        return True

    async def close_position(self, position: Position, exit_price: float):
        position.close_position(exit_price)

        await self.position_storage.add_closed_position(position)
        await self.position_storage.remove_active_position(position.symbol)

    async def create_position(self, event: Union[LongGo, ShortGo]) -> Position:
        account_size = await self.datasource.account_size()
        trading_fee, min_position_size, position_precision, price_precision = await self.datasource.fee_and_precisions(event.symbol)

        stop_loss_price = round(event.stop_loss, price_precision) if event.stop_loss else None
        entry_price = round(event.entry, price_precision)

        size = PositionSizer.calculate_position_size(
            account_size,
            entry_price,
            trading_fee,
            min_position_size,
            position_precision,
            self.leverage,
            stop_loss_price,
            self.risk_per_trade
        )

        position_side = PositionSide.LONG if isinstance(event, LongGo) else PositionSide.SHORT

        return Position(
            symbol=event.symbol,
            timeframe=event.timeframe,
            strategy=event.strategy,
            size=size,
            entry=entry_price,
            side=position_side,
            risk_reward_ratio=event.risk_reward_ratio,
            stop_loss=stop_loss_price
        )

    async def update_position_performance(self, position: Position):
        closed_positions = await self.position_storage.filter_closed_positions_by_strategy(position.strategy_id)
        portfolio_performance = await asyncio.to_thread(self.analytics.calculate, closed_positions)

        await self.dispatcher.dispatch(
            PortfolioPerformanceEvent(strategy_id=position.strategy_id, performance=portfolio_performance))

    def create_open_position_event(self, position: Position) -> Union[LongPositionOpened, ShortPositionOpened, None]:
        if position.side == PositionSide.LONG:
            return LongPositionOpened(
                symbol=position.symbol,
                timeframe=position.timeframe,
                size=position.size,
                entry=position.entry_price,
                stop_loss=position.stop_loss_price
            )
        elif position.side == PositionSide.SHORT:
            return ShortPositionOpened(
                symbol=position.symbol,
                timeframe=position.timeframe,
                size=position.size,
                entry=position.entry_price,
                stop_loss=position.stop_loss_price
            )

    def can_close_position(self, position_side: PositionSide, entry: float, event: Union[LongExit, ShortExit, RiskExit], profit_threshold: float = 0.05) -> bool:
        if isinstance(event, LongExit) and (position_side == PositionSide.LONG):
            if (entry < event.exit) and abs(entry - event.exit) >= profit_threshold:
                return True

        if isinstance(event, ShortExit) and (position_side == PositionSide.SHORT):
            if (entry > event.exit) and abs(entry - event.exit) >= profit_threshold:
                return True

        if isinstance(event, RiskExit) and (position_side == event.side):
            return True

        return False

    async def process_event(self, event: PortfolioEvent):
        symbol = event.symbol

        async with self.state_lock:
            state = self.state.get(symbol, PositionState.IDLE)

        handler = self._state_handlers.get((state, type(event)))

        if handler is None or not await handler(event):
            return

        async with self.state_lock:
            self.state[symbol] = self.next_state(state, event)

    def next_state(self, state: PositionState, event: PortfolioEvent) -> PositionState:
        next_state_mapping = {
            PositionState.IDLE: {
                LongGo: PositionState.OPENING,
                ShortGo: PositionState.OPENING
            },
            PositionState.OPENING: {
                OrderFilled: PositionState.OPENED
            },
            PositionState.OPENED: {
                LongExit: PositionState.CLOSING,
                ShortExit: PositionState.CLOSING,
                RiskExit: PositionState.CLOSING,
                OHLCVEvent: PositionState.OPENED
            },
            PositionState.CLOSING: {
                PositionClosed: PositionState.IDLE
            }
        }

        return next_state_mapping[state].get(type(event), state)
