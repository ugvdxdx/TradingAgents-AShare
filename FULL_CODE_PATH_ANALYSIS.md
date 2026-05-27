# Complete Code Path Analysis: CLI Without API Server

## 1. run_analysis.py (Lines 1-107) — THE WORKING CLI ENTRY POINT

**File**: `/Users/bilibili/Desktop/J-TradingAgents/run_analysis.py`

### Entry Point Flow:
```python
async def main():
    # Lines 10-12: Read CLI arguments
    ticker = sys.argv[1] if len(sys.argv) > 1 else "600519.SH"
    date = sys.argv[2] if len(sys.argv) > 2 else "2026-05-26"
    query = sys.argv[3] if len(sys.argv) > 3 else f"分析{ticker}短线趋势"

    # Line 15: Initialize TradingAgentsGraph
    ta = TradingAgentsGraph()

    # Line 18: Collect market data
    ta.data_collector.collect(ticker, date)

    # Lines 21-22: Parse user intent from query string
    user_intent = parse_intent(query, ta.quick_thinking_llm, fallback_ticker=ticker)

    # Lines 24-26: Create initial state
    state = ta.propagator.create_initial_state(
        ticker, date, user_intent=user_intent, horizon="short"
    )

    # Lines 28-31: Setup graph config
    config = {
        "configurable": {"thread_id": f"{ticker}_{date}"},
        "recursion_limit": 100,
    }

    # Lines 36-84: Stream execution via astream()
    async for chunk in ta.graph.astream(state, config=config, stream_mode="updates"):
        # Process each node's output
        # Extracts analyst reports and debate decisions
        # Pretty-prints progress

    # Lines 92-102: Extract final state and print results
    graph_state = ta.graph.get_state(config)
    final = graph_state.values if graph_state else {}
    
    # Prints:
    # - final_trade_decision
    # - investment_plan
    # - trader_investment_plan

    # Line 104: Free memory
    ta.data_collector.evict(ticker, date)
```

**Key Methods Called**:
- `TradingAgentsGraph()` — constructor (no args)
- `ta.data_collector.collect(ticker, date)` — pre-fetch data once
- `parse_intent(query, llm, fallback_ticker)` → Dict[str, Any]
- `ta.propagator.create_initial_state(ticker, date, user_intent, horizon)` → Dict[str, Any]
- `ta.graph.astream(state, config, stream_mode)` — async generator
- `ta.graph.get_state(config)` — retrieve final state
- `ta.data_collector.evict(ticker, date)` — cleanup

---

## 2. trading_graph.py — Graph Initialization & Core Methods

**File**: `/Users/bilibili/Desktop/J-TradingAgents/tradingagents/graph/trading_graph.py`

### Constructor: Lines 57-151

```python
def __init__(
    self,
    selected_analysts=["market", "social", "news", "fundamentals", "macro", "smart_money", "volume_price"],
    debug=False,
    config: Dict[str, Any] = None,
    callbacks: Optional[List] = None,
    data_collector: Optional["DataCollector"] = None,
):
```

**State Initialization** (lines 66-71):
- `self.config = config or DEFAULT_CONFIG`
- `self.callbacks = callbacks or []`
- Calls `set_config(self.config)` to propagate to dataflows

**Persistence** (lines 74-77):
- Uses `MemorySaver()` (singleton) for graph checkpointing
- Shared across concurrent runs (concurrency-safe)

**LLM Clients** (lines 92-106):
- Creates two LLM clients via `create_llm_client()`:
  - `deep_thinking_llm` (config["deep_think_llm"])
  - `quick_thinking_llm` (config["quick_think_llm"])
- Both support provider-specific kwargs (Google thinking, OpenAI reasoning, etc.)

**Memory Objects** (lines 109-113):
- 5 `FinancialSituationMemory` instances:
  - bull_memory, bear_memory, trader_memory, invest_judge_memory, risk_manager_memory

**Tool Nodes** (line 116):
- `self.tool_nodes = self._create_tool_nodes()`
- Returns Dict[str, ToolNode] with keys: "market", "social", "news", "fundamentals", "macro", "smart_money", "volume_price"

**DataCollector** (line 119):
```python
self.data_collector = data_collector if data_collector is not None else DataCollector()
```

**Graph Setup** (lines 122-151):
```python
self.graph_setup = GraphSetup(...)
self.propagator = Propagator(max_recur_limit=...)
self.reflector = Reflector(...)
self.signal_processor = SignalProcessor(...)
self.graph = self.graph_setup.setup_graph(selected_analysts, checkpointer=self.checkpointer)
```

### propagate() Method: Lines 243-295

```python
def propagate(
    self,
    company_name,
    trade_date,
    user_context: Optional[Dict[str, Any]] = None,
    selected_analysts: Optional[List[str]] = None,
    request_source: str = "api",
    thread_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], Any]:  # (final_state, processed_signal)
```

**Flow**:
1. `init_agent_state = self.propagator.create_initial_state(...)`
2. `args = self.propagator.get_graph_args()`
3. Sets up `args["config"]["configurable"]["thread_id"]`
4. Calls `self.graph.invoke(init_agent_state, **args)` or `self.graph.stream(...)` (debug mode)
5. Returns `(final_state, self.process_signal(final_state["final_trade_decision"]))`

### propagate_async() Method: Lines 297-350

```python
async def propagate_async(
    self,
    company_name: str,
    trade_date: str,
    query: Optional[str] = None,
) -> Dict[str, Any]:
```

**Returns** (lines 346-350):
```python
{
    "short_term": result,
    "medium_term": None,
    "user_intent": user_intent,
}
```

---

## 3. propagation.py (propagator.py) — State Initialization

**File**: `/Users/bilibili/Desktop/J-TradingAgents/tradingagents/graph/propagation.py`

### create_initial_state() Method: Lines 30-111

```python
def create_initial_state(
    self,
    company_name: str,
    trade_date: str,
    user_context: Optional[Mapping[str, Any]] = None,
    selected_analysts: Optional[List[str]] = None,
    request_source: str = "api",
    user_intent: Optional[Dict[str, Any]] = None,
    horizon: str = "short",
) -> Dict[str, Any]:
```

**Returns**: Full state dict with ~50 keys including:
- company_of_interest, trade_date, instrument_context, market_context, user_context
- investment_debate_state, risk_debate_state, risk_feedback_state
- All analyst report fields (market_report, sentiment_report, etc.)
- Investment decision fields (final_trade_decision, investment_plan, etc.)

### get_graph_args() Method: Lines 113-126

```python
def get_graph_args(self, callbacks: Optional[List] = None) -> Dict[str, Any]:
    """Return dict with 'stream_mode' and 'config' keys."""
    return {
        "stream_mode": "values",
        "config": {"recursion_limit": self.max_recur_limit},
    }
```

---

## 4. intent_parser.py — Query Parsing

**File**: `/Users/bilibili/Desktop/J-TradingAgents/tradingagents/graph/intent_parser.py`

### parse_intent() Function: Lines 20-65

```python
def parse_intent(
    query: str,
    llm,
    fallback_ticker: Optional[str] = None,
) -> Dict[str, Any]:
```

**Returns**:
```python
{
    "raw_query": query,
    "ticker": str,
    "horizons": ["short"],  # Fixed
    "focus_areas": list,
    "specific_questions": list,
    "user_context": dict,
}
```

**Stock Name Extraction**: Lines 113-204
- Fallback extraction via regex from query string
- Extracts: objective, risk_profile, investment_horizon, position_pct, cash_available, etc.

---

## 5. cli/main.py — THE BROKEN CLI (Bug at Line 64)

**File**: `/Users/bilibili/Desktop/J-TradingAgents/cli/main.py`

### analyze() Command: Lines 36-77

```python
@app.command()
def analyze(symbol: str, date: Optional[str], horizon: str, query: Optional[str], quick: bool):
    ...
    graph = TradingAgentsGraph(config)
    result = graph.run(resolved, trade_date)  # ← BUG: graph.run() does NOT exist!
```

**THE BUG**: `graph.run()` method does not exist in TradingAgentsGraph class.

**Correct methods**:
- `graph.propagate(company_name, trade_date)` → (final_state, processed_signal)
- `await graph.propagate_async(company_name, trade_date, query)` → full result dict
- But analyze() is NOT async, so would need refactoring

### Symbol Resolution: Lines 173-192

```python
def _resolve_symbol(raw: str) -> Optional[str]:
    # Check for proper format (.SH/.SZ/.BJ)
    if raw.endswith((".SH", ".SZ", ".BJ")):
        return raw
    
    # 6-digit code → add suffix
    if len(raw) == 6 and raw.isdigit():
        if raw.startswith("6"):
            return f"{raw}.SH"
        elif raw.startswith(("0", "3")):
            return f"{raw}.SZ"
        elif raw.startswith(("8", "4")):
            return f"{raw}.BJ"
        return f"{raw}.SH"
    
    # Name lookup via api.main._search_cn_stock_by_name()
    try:
        from api.main import _search_cn_stock_by_name
        return _search_cn_stock_by_name(raw)
    except Exception:
        return None
```

### API Dependencies

**Watchlist Commands** (lines 82-122):
- Import: `api.database.get_db_ctx`, `api.services.watchlist_service`
- Functions: `list_watchlist()`, `add_watchlist_items()`, `delete_watchlist_item()`

**Scheduled Commands** (lines 126-168):
- Import: `api.database.get_db_ctx`, `api.services.scheduled_service`
- Functions: `list_scheduled()`, `create_scheduled()`, `delete_scheduled()`

**Critical**: These require database context (cannot work standalone).

---

## 6. api/main.py — Stock Name Mapping (Lines 353-445)

**File**: `/Users/bilibili/Desktop/J-TradingAgents/api/main.py`

### _load_cn_stock_map() — Lines 353-406

```python
def _load_cn_stock_map() -> Dict[str, str]:
    """Lazy-load A-share stock + ETF/fund name→code mapping (7-day TTL)."""
    # Cached globally with threading lock
    # Uses akshare.stock_info_a_code_name() + fund_name_em()
    # Returns: {"股票名称": "600519.SH", ...}
```

**Global State** (lines 309-350):
```python
_cn_stock_map: Optional[Dict[str, str]] = None
_cn_stock_reverse_map: Optional[Dict[str, str]] = None
_cn_stock_map_lock = Lock()
_cn_stock_map_loaded_at: float = 0
_STOCK_MAP_TTL = 7 * 86400
```

### _search_cn_stock_by_name() — Lines 427-445

```python
def _search_cn_stock_by_name(query: str) -> Optional[str]:
    """Exact → partial → shortest name match."""
    stock_map = _load_cn_stock_map()
    
    # 1. Exact match
    if query in stock_map:
        return stock_map[query]
    
    # 2. Substring match (query in name or name in query)
    candidates = [(name, code) for name, code in stock_map.items()
                  if query in name or name in query]
    if len(candidates) == 1:
        return candidates[0][1]
    
    # 3. Multiple: pick shortest name (best match)
    if candidates:
        candidates.sort(key=lambda x: len(x[0]))
        return candidates[0][1]
    
    return None
```

### _normalize_symbol() — Lines 2022-2045

```python
def _normalize_symbol(raw: str) -> str:
    """Convert to XXXXXX.SH|SZ|BJ format."""
    s = raw.strip().upper()
    
    # 1. 6-digit code with optional market suffix
    m = re.search(r"(\d{6})(?:\.(SH|SZ|SS))?", s)
    if m:
        code = m.group(1)
        suffix = m.group(2)
        if suffix:
            return f"{code}.SH" if suffix == "SS" else f"{code}.{suffix}"
        # Infer from code: 5,6,9→SH; others→SZ
        market = "SH" if code.startswith(("5", "6", "9")) else "SZ"
        return f"{code}.{market}"
    
    # 2. Letter ticker
    m2 = re.search(r"([A-Z]{1,6}(?:\.[A-Z]{1,3})?)", s)
    if m2:
        return m2.group(1)
    
    # 3. Chinese name lookup
    stock_map = _load_cn_stock_map()
    if s in stock_map:
        return stock_map[s]
    
    return s
```

---

## 7. watchlist_service.py — Watchlist Operations

**File**: `/Users/bilibili/Desktop/J-TradingAgents/api/services/watchlist_service.py`

### Functions & Signatures:

```python
def list_watchlist(db: Session, user_id: str) -> List[dict]
def add_watchlist_item(db: Session, user_id: str, symbol: str) -> dict
def add_watchlist_items(db: Session, user_id: str, symbols: List[str]) -> List[dict]
def delete_watchlist_item(db: Session, user_id: str, item_id: str) -> bool
```

**Constraint**: MAX_WATCHLIST_ITEMS = 50

**Database Required**: YES (SQLAlchemy Session)

---

## 8. scheduled_service.py — Scheduled Tasks

**File**: `/Users/bilibili/Desktop/J-TradingAgents/api/services/scheduled_service.py`

### Functions & Signatures:

```python
def list_scheduled(db: Session, user_id: str) -> List[dict]
def create_scheduled(db: Session, user_id: str, symbol: str, horizon: str, trigger_time: str) -> dict
def delete_scheduled(db: Session, user_id: str, item_id: str) -> bool
def _validate_trigger_time(t: str) -> str  # HH:MM format; 20:00-23:59 or 00:00-08:00
```

**Constraints**:
- MAX_SCHEDULED_ITEMS = 10
- VALID_HORIZONS = {"short", "medium"}
- Trigger times: 20:00-23:59 or 00:00-08:00 (avoid trading hours)

**Database Required**: YES (SQLAlchemy Session)

---

## SUMMARY TABLE

| Component | Location | Can Run Standalone? | Needs Database? | Needs API Server? |
|-----------|----------|-----|-----|-----|
| run_analysis.py | - | **YES** | NO | NO |
| TradingAgentsGraph.__init__() | trading_graph.py:57 | **YES** | NO | NO |
| propagate() | trading_graph.py:243 | **YES** | NO | NO |
| propagate_async() | trading_graph.py:297 | **YES** | NO | NO |
| Propagator.create_initial_state() | propagation.py:30 | **YES** | NO | NO |
| parse_intent() | intent_parser.py:20 | **YES** | NO | NO |
| _normalize_symbol() | api/main.py:2022 | **YES** | NO | NO |
| _search_cn_stock_by_name() | api/main.py:427 | **YES** | NO | NO |
| _load_cn_stock_map() | api/main.py:353 | **YES** | NO | NO |
| cli/main.py analyze() | cli/main.py:36 | **NO** (has bug line 64) | NO | NO |
| watchlist commands | cli/main.py:82 | **NO** | **YES** | Requires DB context |
| scheduled commands | cli/main.py:126 | **NO** | **YES** | Requires DB context |

---

## KEY FINDINGS

### Working Code Path (run_analysis.py):

1. Parse CLI args (ticker, date, query)
2. Create TradingAgentsGraph() — loads LLM, initializes state
3. data_collector.collect(ticker, date) — fetch data once
4. parse_intent(query, llm) — extract intent
5. propagator.create_initial_state(...) — build state dict
6. ta.graph.astream(state, config) — stream execution with progress
7. ta.graph.get_state(config) — extract final results
8. data_collector.evict(ticker, date) — cleanup

**Zero HTTP/database calls required.**

### The Bug in cli/main.py (Line 64):

```python
graph = TradingAgentsGraph(config)
result = graph.run(resolved, trade_date)  # ← DOES NOT EXIST
```

Should be:
```python
result, signal = graph.propagate(resolved, trade_date)
# OR
# result = await graph.propagate_async(resolved, trade_date)
# (requires making analyze() async)
```

### Stock Name Resolution Flow:

```
cli/_resolve_symbol(symbol)
  ├─ If XXXXXX.SH|SZ|BJ format: return as-is
  ├─ If 6 digits: infer market and add suffix
  └─ Else: call api.main._search_cn_stock_by_name(symbol)
      └─ api.main._load_cn_stock_map() [akshare + 7-day cache]
          └─ Returns code → symbol mapping
```

### Pure Utility Functions (No HTTP/DB):

1. **_normalize_symbol(raw: str) → str** (lines 2022-2045)
2. **_search_cn_stock_by_name(query: str) → Optional[str]** (lines 427-445)
3. **_load_cn_stock_map() → Dict[str, str]** (lines 353-406)

These can be extracted and reused in CLI without API dependency.

