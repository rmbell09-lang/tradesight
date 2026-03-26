"""
TradeSight AI Strategy Iteration Engine

Core AI system that can take a trading strategy and iteratively improve it
through backtesting and AI-driven parameter optimization.
"""

import pandas as pd
import numpy as np
import json
import random
import copy
from typing import Dict, List, Callable, Any, Optional, Tuple
from datetime import datetime, timedelta
import inspect
import re
from dataclasses import dataclass, asdict

from .backtest import BacktestEngine, simple_ma_crossover, rsi_mean_reversion


@dataclass 
class StrategyGeneration:
    """Represents one generation/iteration of a strategy"""
    generation: int
    strategy_code: str
    parameters: Dict[str, Any]
    backtest_results: Dict[str, Any]
    performance_score: float
    mutation_from: Optional[int] = None
    mutation_type: Optional[str] = None


@dataclass
class MultiAssetResults:
    """Results from testing a strategy across multiple assets"""
    strategy_generation: int
    asset_results: Dict[str, Dict]  # asset_name -> backtest_results
    average_performance: float
    consistency_score: float  # How consistent performance is across assets
    risk_adjusted_score: float
    passes_validation: bool


class AIStrategyEngine:
    """
    AI-powered strategy iteration engine that can automatically improve
    trading strategies through backtesting and parameter optimization.
    
    Implements the Michael Automates workflow:
    1. Start with base strategy
    2. Backtest on primary asset
    3. AI modifies parameters/logic
    4. Re-backtest and compare
    5. Keep improvements, discard failures
    6. After N iterations, test best on multiple assets
    7. Only strategies that work across assets survive
    """
    
    def __init__(self, initial_balance: float = 500.0, max_generations: int = 20):
        self.initial_balance = initial_balance
        self.max_generations = max_generations
        self.backtest_engine = BacktestEngine(initial_balance)
        self.generations: List[StrategyGeneration] = []
        self.best_strategy = None
        self.iteration_history = []
        
        # AI mutation strategies
        self.mutation_strategies = [
            'parameter_tweak',
            'threshold_adjust', 
            'condition_modify',
            'risk_management_adjust',
            'indicator_parameter_tune'
        ]
    
    def evolve_strategy(self,
                       base_strategy_func: Callable,
                       training_data: pd.DataFrame,
                       asset_name: str = "BTC",
                       target_generations: int = None) -> StrategyGeneration:
        """
        Main evolution loop - takes base strategy and evolves it over multiple generations.
        
        Args:
            base_strategy_func: Initial strategy function to improve
            training_data: Historical OHLCV data for backtesting
            asset_name: Name of asset for logging
            target_generations: Override default max_generations
            
        Returns:
            Best performing strategy generation
        """
        print(f"🚀 Starting AI strategy evolution on {asset_name}")
        print(f"📊 Training data: {len(training_data)} bars")
        
        generations = target_generations or self.max_generations
        self.generations = []
        
        # Generation 0: Baseline strategy
        baseline_result = self._evaluate_strategy(base_strategy_func, training_data, asset_name)
        
        gen_0 = StrategyGeneration(
            generation=0,
            strategy_code=inspect.getsource(base_strategy_func),
            parameters=self._extract_parameters(base_strategy_func),
            backtest_results=baseline_result,
            performance_score=self._calculate_performance_score(baseline_result),
            mutation_from=None,
            mutation_type="baseline"
        )
        
        self.generations.append(gen_0)
        self.best_strategy = gen_0
        
        print(f"📈 Generation 0 (Baseline): Score {gen_0.performance_score:.2f}, "
              f"Win Rate {baseline_result['metrics']['win_rate']:.1f}%, "
              f"Total PnL {baseline_result['metrics']['total_pnl_pct']:.2f}%")
        
        # Evolution loop
        for gen in range(1, generations + 1):
            print(f"\n🔬 Generation {gen}: Mutating best strategy...")
            
            # Create multiple mutation candidates
            candidates = []
            for i in range(3):  # Try 3 mutations per generation
                try:
                    mutated_func = self._mutate_strategy(self.best_strategy)
                    if mutated_func:
                        candidates.append(mutated_func)
                except Exception as e:
                    print(f"⚠️ Mutation {i+1} failed: {str(e)[:100]}")
                    continue
            
            if not candidates:
                print(f"❌ No valid mutations generated for generation {gen}")
                continue
            
            # Test all candidates and pick the best
            best_candidate = None
            best_score = self.best_strategy.performance_score
            
            for i, candidate in enumerate(candidates):
                try:
                    result = self._evaluate_strategy(candidate, training_data, asset_name)
                    score = self._calculate_performance_score(result)
                    
                    print(f"   Candidate {i+1}: Score {score:.2f}, "
                          f"Win Rate {result['metrics']['win_rate']:.1f}%, "
                          f"PnL {result['metrics']['total_pnl_pct']:.2f}%")
                    
                    if score > best_score:
                        best_candidate = candidate
                        best_score = score
                        best_result = result
                        
                except Exception as e:
                    print(f"   Candidate {i+1}: FAILED - {str(e)[:100]}")
                    continue
            
            # If we found an improvement, record it
            if best_candidate and best_score > self.best_strategy.performance_score:
                new_generation = StrategyGeneration(
                    generation=gen,
                    strategy_code=inspect.getsource(best_candidate),
                    parameters=self._extract_parameters(best_candidate),
                    backtest_results=best_result,
                    performance_score=best_score,
                    mutation_from=self.best_strategy.generation,
                    mutation_type="improved"
                )
                
                self.generations.append(new_generation)
                self.best_strategy = new_generation
                
                improvement = best_score - self.generations[0].performance_score
                print(f"✅ IMPROVEMENT! New best score: {best_score:.2f} "
                      f"(+{improvement:.2f} from baseline)")
            else:
                print(f"➖ No improvement in generation {gen}")
        
        # Final summary
        final_improvement = self.best_strategy.performance_score - self.generations[0].performance_score
        improvement_pct = (final_improvement / self.generations[0].performance_score) * 100 if self.generations[0].performance_score != 0 else 0
        
        print(f"\n🏁 Evolution complete!")
        print(f"📊 Final best strategy: Generation {self.best_strategy.generation}")
        print(f"🎯 Performance improvement: {improvement_pct:.1f}% over baseline")
        print(f"📈 Final metrics: Win Rate {self.best_strategy.backtest_results['metrics']['win_rate']:.1f}%, "
              f"PnL {self.best_strategy.backtest_results['metrics']['total_pnl_pct']:.2f}%")
        
        return self.best_strategy
    
    def validate_across_assets(self,
                              strategy_generation: StrategyGeneration,
                              asset_datasets: Dict[str, pd.DataFrame]) -> MultiAssetResults:
        """
        Test the evolved strategy across multiple assets to ensure it's not overfit.
        
        Args:
            strategy_generation: The evolved strategy to test
            asset_datasets: Dict of asset_name -> OHLCV DataFrame
            
        Returns:
            MultiAssetResults with cross-asset performance
        """
        print(f"\n🌍 Multi-asset validation for Generation {strategy_generation.generation}")
        
        # Recreate strategy function from code (simplified - in real implementation
        # would need proper code parsing and dynamic function creation)
        strategy_func = self._code_to_function(strategy_generation.strategy_code)
        
        asset_results = {}
        scores = []
        
        for asset_name, data in asset_datasets.items():
            print(f"   Testing on {asset_name}...")
            try:
                result = self._evaluate_strategy(strategy_func, data, asset_name)
                score = self._calculate_performance_score(result)
                
                asset_results[asset_name] = result
                scores.append(score)
                
                print(f"   {asset_name}: Score {score:.2f}, "
                      f"Win Rate {result['metrics']['win_rate']:.1f}%, "
                      f"PnL {result['metrics']['total_pnl_pct']:.2f}%")
                      
            except Exception as e:
                print(f"   {asset_name}: FAILED - {str(e)[:100]}")
                asset_results[asset_name] = None
                scores.append(0.0)
        
        # Calculate aggregate metrics
        valid_scores = [s for s in scores if s > 0]
        average_performance = np.mean(valid_scores) if valid_scores else 0.0
        consistency_score = 1.0 - (np.std(valid_scores) / np.mean(valid_scores)) if valid_scores and np.mean(valid_scores) > 0 else 0.0
        
        # Risk-adjusted score combines average performance with consistency
        risk_adjusted_score = average_performance * consistency_score
        
        # Pass validation if: average > 0, at least 70% of assets profitable, consistency > 0.3
        profitable_assets = sum(1 for s in valid_scores if s > 0)
        total_tested = len([r for r in asset_results.values() if r is not None])
        
        passes_validation = (
            average_performance > 0 and
            (profitable_assets / total_tested) >= 0.7 if total_tested > 0 else False and
            consistency_score > 0.3
        )
        
        print(f"\n📊 Multi-asset results:")
        print(f"   Average Performance: {average_performance:.2f}")
        print(f"   Consistency Score: {consistency_score:.2f}")
        print(f"   Risk-Adjusted Score: {risk_adjusted_score:.2f}")
        print(f"   Profitable Assets: {profitable_assets}/{total_tested}")
        print(f"   Validation Status: {'✅ PASSED' if passes_validation else '❌ FAILED'}")
        
        return MultiAssetResults(
            strategy_generation=strategy_generation.generation,
            asset_results=asset_results,
            average_performance=average_performance,
            consistency_score=consistency_score,
            risk_adjusted_score=risk_adjusted_score,
            passes_validation=passes_validation
        )
    
    def _evaluate_strategy(self, strategy_func: Callable, data: pd.DataFrame, asset_name: str) -> Dict[str, Any]:
        """Backtest a strategy function and return results"""
        return self.backtest_engine.run_backtest(data, strategy_func, asset_name)
    
    def _calculate_performance_score(self, backtest_result: Dict[str, Any]) -> float:
        """
        Calculate single performance score from backtest results.
        Combines profitability, risk management, and consistency.
        """
        metrics = backtest_result['metrics']
        
        if metrics['total_trades'] == 0:
            return -1.0
        
        # Base score from PnL percentage
        pnl_score = metrics['total_pnl_pct'] / 100.0
        
        # Win rate bonus (0.5-1.5x multiplier)
        win_rate_mult = 0.5 + (metrics['win_rate'] / 100.0)
        
        # Drawdown penalty (max 50% reduction)
        drawdown_penalty = max(0.5, 1.0 - (metrics['max_drawdown'] / 50.0))
        
        # Trade count bonus (more trades = more statistical significance)
        trade_bonus = min(1.5, 1.0 + (metrics['total_trades'] / 100.0))
        
        # Profit factor consideration
        pf_bonus = min(2.0, metrics['profit_factor'] / 2.0) if metrics['profit_factor'] != float('inf') else 2.0
        
        final_score = pnl_score * win_rate_mult * drawdown_penalty * trade_bonus * (pf_bonus * 0.2)
        
        return max(0.0, final_score)
    
    def _extract_parameters(self, strategy_func: Callable) -> Dict[str, Any]:
        """Extract adjustable parameters from strategy function code"""
        source = inspect.getsource(strategy_func)
        parameters = {}
        
        # Look for numeric literals that could be parameters
        # RSI thresholds
        rsi_matches = re.findall(r"rsi['\"]?\s*[<>]=?\s*(\d+)", source)
        if rsi_matches:
            parameters['rsi_thresholds'] = [int(x) for x in rsi_matches]
        
        # Moving average periods
        ma_matches = re.findall(r"sma_(\d+)", source)
        if ma_matches:
            parameters['ma_periods'] = [int(x) for x in ma_matches]
        
        # Stop loss/take profit percentages
        sl_matches = re.findall(r"\*\s*(0\.\d+)", source)
        if sl_matches:
            parameters['risk_percentages'] = [float(x) for x in sl_matches]
        
        # Position sizes
        size_matches = re.findall(r"'size':\s*(0\.\d+)", source)
        if size_matches:
            parameters['position_sizes'] = [float(x) for x in size_matches]
        
        return parameters
    
    def _mutate_strategy(self, strategy_gen: StrategyGeneration) -> Optional[Callable]:
        """
        Create a mutated version of the strategy with modified parameters.
        
        This is a simplified version - real implementation would need more
        sophisticated code parsing and modification capabilities.
        """
        mutation_type = random.choice(self.mutation_strategies)
        source_code = strategy_gen.strategy_code
        
        try:
            if mutation_type == 'parameter_tweak':
                # Modify numeric parameters by small amounts
                modified_code = self._tweak_numeric_parameters(source_code)
            elif mutation_type == 'threshold_adjust':
                # Adjust RSI/indicator thresholds
                modified_code = self._adjust_thresholds(source_code)
            elif mutation_type == 'risk_management_adjust':
                # Modify stop loss/take profit levels
                modified_code = self._adjust_risk_levels(source_code)
            else:
                # Default: small parameter tweaks
                modified_code = self._tweak_numeric_parameters(source_code)
            
            # Create new function from modified code
            if modified_code and modified_code != source_code:
                return self._code_to_function(modified_code)
            
        except Exception as e:
            print(f"Mutation failed: {e}")
            
        return None
    
    def _tweak_numeric_parameters(self, source_code: str) -> str:
        """Randomly adjust numeric parameters in strategy code"""
        # Find and modify RSI thresholds
        def adjust_rsi(match):
            value = int(match.group(1))
            # Adjust by ±5
            new_value = value + random.randint(-5, 5)
            new_value = max(10, min(90, new_value))  # Keep within reasonable bounds
            return match.group(0).replace(match.group(1), str(new_value))
        
        modified = re.sub(r"(rsi['\"]?\s*[<>]=?\s*)(\d+)", adjust_rsi, source_code)
        
        # Find and modify percentage multipliers (stop loss, take profit)
        def adjust_percentage(match):
            value = float(match.group(1))
            # Adjust by ±0.02 (2%)
            adjustment = random.uniform(-0.02, 0.02)
            new_value = max(0.85, min(1.20, value + adjustment))  # Keep reasonable
            return match.group(0).replace(match.group(1), f"{new_value:.3f}")
        
        modified = re.sub(r"(\*\s*)(0\.\d+)", adjust_percentage, modified)
        
        # Find and modify position sizes
        def adjust_size(match):
            value = float(match.group(1))
            # Adjust by ±0.1
            new_value = max(0.1, min(1.0, value + random.uniform(-0.1, 0.1)))
            return match.group(0).replace(match.group(1), f"{new_value:.1f}")
        
        modified = re.sub(r"('size':\s*)(0\.\d+)", adjust_size, modified)
        
        return modified
    
    def _adjust_thresholds(self, source_code: str) -> str:
        """Specifically adjust indicator thresholds"""
        modified = source_code
        
        # RSI oversold/overbought levels
        modified = re.sub(r"< 30", f"< {random.randint(25, 35)}", modified)
        modified = re.sub(r"> 70", f"> {random.randint(65, 75)}", modified)
        
        return modified
    
    def _adjust_risk_levels(self, source_code: str) -> str:
        """Adjust stop loss and take profit levels"""
        modified = source_code
        
        # Stop loss levels (typically 0.93-0.97 range)
        def adjust_sl(match):
            base = random.uniform(0.92, 0.98)
            return f"* {base:.3f}"
        
        modified = re.sub(r"\* 0\.9[3-7]", adjust_sl, modified)
        
        # Take profit levels (typically 1.05-1.15 range)
        def adjust_tp(match):
            base = random.uniform(1.05, 1.15)
            return f"* {base:.3f}"
        
        modified = re.sub(r"\* 1\.0[5-9]", adjust_tp, modified)
        modified = re.sub(r"\* 1\.1[0-5]", adjust_tp, modified)
        
        return modified
    
    def _code_to_function(self, code: str) -> Callable:
        """
        Convert strategy code string back to executable function.
        
        In a real implementation, this would need more sophisticated
        parsing and sandboxed execution.
        """
        # Extract function name from code
        func_name_match = re.search(r"def\s+(\w+)\s*\(", code)
        if not func_name_match:
            raise ValueError("Cannot find function definition in code")
        
        func_name = func_name_match.group(1)
        
        # Create namespace with required imports
        namespace = {
            'pd': pd,
            'np': np,
            'Optional': Optional,
            'Dict': Dict,
            'List': List
        }
        
        # Execute the code to create the function
        exec(code, namespace)
        
        return namespace[func_name]
    
    def get_evolution_summary(self) -> Dict[str, Any]:
        """Get summary of the evolution process"""
        if not self.generations:
            return {}
        
        baseline = self.generations[0]
        final = self.best_strategy
        
        improvement = final.performance_score - baseline.performance_score
        improvement_pct = (improvement / baseline.performance_score) * 100 if baseline.performance_score > 0 else 0
        
        return {
            'total_generations': len(self.generations),
            'baseline_score': baseline.performance_score,
            'final_score': final.performance_score,
            'improvement_absolute': improvement,
            'improvement_percentage': improvement_pct,
            'baseline_metrics': baseline.backtest_results['metrics'],
            'final_metrics': final.backtest_results['metrics'],
            'successful_mutations': sum(1 for g in self.generations if g.mutation_type == "improved"),
            'evolution_path': [g.generation for g in self.generations]
        }


# Example usage and testing functions
def create_test_data(symbol: str = "BTC", days: int = 365) -> pd.DataFrame:
    """Create realistic test data for backtesting"""
    np.random.seed(42)  # Reproducible results
    
    dates = pd.date_range(start='2023-01-01', periods=days, freq='D')
    
    # Generate realistic price movement with some trend
    base_price = 50000 if symbol == "BTC" else 100
    returns = np.random.normal(0.001, 0.02, days)  # Slight positive drift, 2% daily volatility
    
    # Add some autocorrelation for realism
    for i in range(1, len(returns)):
        returns[i] += 0.1 * returns[i-1]
    
    prices = [base_price]
    for ret in returns[1:]:
        prices.append(prices[-1] * (1 + ret))
    
    # Create OHLCV data
    data = []
    for i, (date, close) in enumerate(zip(dates, prices)):
        high = close * random.uniform(1.001, 1.02)
        low = close * random.uniform(0.98, 0.999)
        open_price = random.uniform(low, high)
        volume = random.randint(1000000, 10000000)
        
        data.append({
            'open': open_price,
            'high': high,
            'low': low,
            'close': close,
            'volume': volume
        })
    
    df = pd.DataFrame(data, index=dates)
    return df


def example_evolution():
    """Example of running the AI strategy evolution"""
    
    # Create AI engine
    ai_engine = AIStrategyEngine(initial_balance=500, max_generations=5)
    
    # Create test data for multiple assets
    btc_data = create_test_data("BTC", 300)
    eth_data = create_test_data("ETH", 300)
    
    print("🚀 Starting AI Strategy Evolution Example")
    
    # Evolve strategy on BTC
    best_strategy = ai_engine.evolve_strategy(
        base_strategy_func=simple_ma_crossover,
        training_data=btc_data,
        asset_name="BTC",
        target_generations=3  # Quick demo
    )
    
    # Validate across multiple assets
    asset_datasets = {
        "BTC": btc_data,
        "ETH": eth_data
    }
    
    validation_results = ai_engine.validate_across_assets(best_strategy, asset_datasets)
    
    # Print summary
    print("\n" + "="*60)
    print("EVOLUTION SUMMARY")
    print("="*60)
    
    summary = ai_engine.get_evolution_summary()
    print(f"Total Generations: {summary['total_generations']}")
    print(f"Performance Improvement: {summary['improvement_percentage']:.1f}%")
    print(f"Baseline Win Rate: {summary['baseline_metrics']['win_rate']:.1f}%")
    print(f"Final Win Rate: {summary['final_metrics']['win_rate']:.1f}%")
    print(f"Multi-Asset Validation: {'PASSED' if validation_results.passes_validation else 'FAILED'}")
    
    return ai_engine, best_strategy, validation_results


if __name__ == "__main__":
    # Run example
    engine, strategy, validation = example_evolution()
