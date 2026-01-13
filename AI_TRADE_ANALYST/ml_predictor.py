#!/usr/bin/env python3
"""
ML Trade Prediction System
Learns from historical trades to predict outcomes and optimize parameters.

Requirements:
    pip install pandas numpy scikit-learn xgboost

Usage:
    python ml_predictor.py --train        # Train model on historical data
    python ml_predictor.py --predict      # Get prediction for current conditions
    python ml_predictor.py --features     # Analyze feature importance
    python ml_predictor.py --backtest     # Backtest model predictions
"""

import os
import sys
import json
import sqlite3
import pickle
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
import warnings
warnings.filterwarnings('ignore')

try:
    import pandas as pd
    import numpy as np
    from sklearn.model_selection import train_test_split, cross_val_score, TimeSeriesSplit
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler, LabelEncoder
    from sklearn.metrics import classification_report, confusion_matrix, roc_auc_score
    HAS_ML = True
except ImportError:
    HAS_ML = False
    print("ML packages not installed. Run: pip install pandas numpy scikit-learn")

try:
    import xgboost as xgb
    HAS_XGB = True
except ImportError:
    HAS_XGB = False


# ============================================================================
# FEATURE ENGINEERING
# ============================================================================

class FeatureEngineer:
    """
    Creates features from raw trade and market data.

    Feature Categories:
    1. Time-based (session, day of week, calendar events)
    2. Statistical (z-score, Hurst, half-life, volatility)
    3. Market context (correlations, positioning)
    4. Trade-specific (direction, lot size, costs)
    """

    # Session definitions (UTC hours)
    SESSIONS = {
        'asian': (0, 8),
        'london': (8, 16),
        'ny': (13, 21),
        'overlap': (13, 16)
    }

    # High impact events
    NFP_TIMES = ['first_friday_13:30']  # First Friday 13:30 UTC

    def __init__(self):
        self.scaler = StandardScaler()
        self.label_encoders = {}

    def extract_time_features(self, timestamp: datetime) -> Dict[str, Any]:
        """Extract time-based features from timestamp"""
        hour = timestamp.hour
        minute = timestamp.minute

        features = {
            # Basic time
            'hour': hour,
            'minute': minute,
            'day_of_week': timestamp.weekday(),
            'day_of_month': timestamp.day,
            'week_of_year': timestamp.isocalendar()[1],
            'month': timestamp.month,

            # Session indicators
            'is_asian': 1 if 0 <= hour < 8 else 0,
            'is_london': 1 if 8 <= hour < 16 else 0,
            'is_ny': 1 if 13 <= hour < 21 else 0,
            'is_overlap': 1 if 13 <= hour < 16 else 0,

            # Special times
            'is_ny_open': 1 if (hour == 13 and minute >= 30) or (hour == 14 and minute < 30) else 0,
            'is_london_open': 1 if 7 <= hour < 9 else 0,
            'is_london_close': 1 if 15 <= hour < 17 else 0,
            'is_after_hours': 1 if hour >= 21 or hour < 1 else 0,

            # Day indicators
            'is_monday': 1 if timestamp.weekday() == 0 else 0,
            'is_friday': 1 if timestamp.weekday() == 4 else 0,
            'is_weekend_adjacent': 1 if timestamp.weekday() in [0, 4] else 0,

            # Calendar events
            'is_nfp_day': 1 if self._is_nfp_day(timestamp) else 0,
            'is_month_end': 1 if timestamp.day >= 28 else 0,
            'is_quarter_end': 1 if timestamp.month in [3, 6, 9, 12] and timestamp.day >= 28 else 0,
        }

        return features

    def _is_nfp_day(self, timestamp: datetime) -> bool:
        """Check if first Friday of month (NFP day)"""
        if timestamp.weekday() != 4:  # Not Friday
            return False
        return timestamp.day <= 7

    def extract_statistical_features(self,
                                      zscore: float,
                                      spread: float,
                                      mean: float,
                                      std: float,
                                      hurst: Optional[float] = None,
                                      half_life: Optional[float] = None) -> Dict[str, float]:
        """Extract statistical features"""
        features = {
            # Z-score features
            'zscore': zscore,
            'zscore_abs': abs(zscore),
            'zscore_squared': zscore ** 2,

            # Spread features
            'spread': spread,
            'spread_deviation': spread - mean,
            'spread_deviation_pct': (spread - mean) / mean * 100 if mean != 0 else 0,

            # Distribution features
            'std': std,
            'mean': mean,
            'cv': std / mean if mean != 0 else 0,  # Coefficient of variation

            # Regime features
            'hurst': hurst if hurst is not None else 0.5,
            'is_mean_reverting': 1 if hurst and hurst < 0.45 else 0,
            'is_trending': 1 if hurst and hurst > 0.55 else 0,

            # Half-life
            'half_life': half_life if half_life is not None else 50,
            'half_life_log': np.log(half_life) if half_life and half_life > 0 else 0,
        }

        return features

    def extract_volatility_features(self,
                                     volatility_1h: float,
                                     volatility_4h: float,
                                     volatility_24h: float) -> Dict[str, float]:
        """Extract volatility features"""
        features = {
            'vol_1h': volatility_1h,
            'vol_4h': volatility_4h,
            'vol_24h': volatility_24h,

            # Relative volatility
            'vol_ratio_1h_4h': volatility_1h / volatility_4h if volatility_4h > 0 else 1,
            'vol_ratio_4h_24h': volatility_4h / volatility_24h if volatility_24h > 0 else 1,

            # Volatility regime
            'vol_expanding': 1 if volatility_1h > volatility_4h > volatility_24h else 0,
            'vol_contracting': 1 if volatility_1h < volatility_4h < volatility_24h else 0,
        }

        return features

    def extract_trade_features(self,
                                direction: str,
                                lot_size: float,
                                entry_zscore: float,
                                cost_per_lot: float) -> Dict[str, float]:
        """Extract trade-specific features"""
        features = {
            'is_long_spread': 1 if direction == 'Long Spread' else 0,
            'is_short_spread': 1 if direction == 'Short Spread' else 0,
            'lot_size': lot_size,
            'lot_size_log': np.log(lot_size) if lot_size > 0 else 0,

            # Entry quality
            'entry_zscore_abs': abs(entry_zscore),
            'entry_strength': abs(entry_zscore) - 3.5,  # How far beyond threshold

            # Cost exposure
            'cost_per_lot': cost_per_lot,
            'total_cost_estimate': cost_per_lot * lot_size,
        }

        return features

    def create_feature_vector(self, trade_data: Dict) -> pd.DataFrame:
        """Create full feature vector from trade data"""
        features = {}

        # Time features
        timestamp = trade_data.get('entry_time', datetime.now())
        if isinstance(timestamp, str):
            timestamp = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
        features.update(self.extract_time_features(timestamp))

        # Statistical features
        features.update(self.extract_statistical_features(
            zscore=trade_data.get('entry_zscore', 0),
            spread=trade_data.get('entry_spread', 0),
            mean=trade_data.get('mean', trade_data.get('entry_spread', 0)),
            std=trade_data.get('std', 1),
            hurst=trade_data.get('hurst'),
            half_life=trade_data.get('half_life')
        ))

        # Volatility features
        features.update(self.extract_volatility_features(
            volatility_1h=trade_data.get('vol_1h', 0.01),
            volatility_4h=trade_data.get('vol_4h', 0.01),
            volatility_24h=trade_data.get('vol_24h', 0.01)
        ))

        # Trade features
        features.update(self.extract_trade_features(
            direction=trade_data.get('direction', 'Long Spread'),
            lot_size=trade_data.get('lot_size', 20),
            entry_zscore=trade_data.get('entry_zscore', 0),
            cost_per_lot=trade_data.get('cost_per_lot', 48)
        ))

        return pd.DataFrame([features])


# ============================================================================
# ML MODEL
# ============================================================================

class TradePredictor:
    """
    ML model for predicting trade outcomes.

    Supported predictions:
    1. Win/Loss classification
    2. Expected P&L regression
    3. Optimal exit timing
    4. Risk-adjusted score
    """

    def __init__(self, model_path: Optional[str] = None):
        self.feature_engineer = FeatureEngineer()
        self.model = None
        self.scaler = StandardScaler()
        self.feature_columns = []
        self.model_path = model_path or 'trade_predictor.pkl'

        # Load existing model if available
        if os.path.exists(self.model_path):
            self.load_model()

    def prepare_training_data(self, trades: List[Dict]) -> Tuple[pd.DataFrame, pd.Series]:
        """Prepare training data from trade history"""
        feature_dfs = []
        labels = []

        for trade in trades:
            # Create features
            features = self.feature_engineer.create_feature_vector(trade)
            feature_dfs.append(features)

            # Create label (1 = winner, 0 = loser)
            labels.append(1 if trade.get('net_pnl', 0) > 0 else 0)

        X = pd.concat(feature_dfs, ignore_index=True)
        y = pd.Series(labels)

        self.feature_columns = X.columns.tolist()

        return X, y

    def train(self, trades: List[Dict], model_type: str = 'xgboost') -> Dict:
        """Train prediction model"""
        if not HAS_ML:
            return {'error': 'ML packages not installed'}

        print(f"\n📊 Training Trade Predictor ({len(trades)} trades)...")

        # Prepare data
        X, y = self.prepare_training_data(trades)

        # Check minimum data
        if len(trades) < 100:
            print(f"⚠️  Warning: Only {len(trades)} trades. Recommend 300+ for reliable predictions.")

        # Scale features
        X_scaled = self.scaler.fit_transform(X)

        # Train/test split (time-aware)
        X_train, X_test, y_train, y_test = train_test_split(
            X_scaled, y, test_size=0.2, shuffle=False  # Don't shuffle for time series
        )

        # Select model
        if model_type == 'xgboost' and HAS_XGB:
            self.model = xgb.XGBClassifier(
                n_estimators=100,
                max_depth=5,
                learning_rate=0.1,
                objective='binary:logistic',
                eval_metric='auc',
                use_label_encoder=False
            )
        elif model_type == 'random_forest':
            self.model = RandomForestClassifier(
                n_estimators=100,
                max_depth=10,
                min_samples_split=10,
                random_state=42
            )
        else:
            self.model = GradientBoostingClassifier(
                n_estimators=100,
                max_depth=5,
                learning_rate=0.1,
                random_state=42
            )

        # Train
        print("   Training model...")
        self.model.fit(X_train, y_train)

        # Evaluate
        y_pred = self.model.predict(X_test)
        y_prob = self.model.predict_proba(X_test)[:, 1]

        # Cross-validation
        cv = TimeSeriesSplit(n_splits=5)
        cv_scores = cross_val_score(self.model, X_scaled, y, cv=cv, scoring='roc_auc')

        results = {
            'accuracy': (y_pred == y_test).mean(),
            'roc_auc': roc_auc_score(y_test, y_prob),
            'cv_mean': cv_scores.mean(),
            'cv_std': cv_scores.std(),
            'train_size': len(X_train),
            'test_size': len(X_test),
            'feature_count': len(self.feature_columns)
        }

        print(f"\n   Results:")
        print(f"   • Accuracy: {results['accuracy']:.2%}")
        print(f"   • ROC-AUC: {results['roc_auc']:.3f}")
        print(f"   • CV Score: {results['cv_mean']:.3f} ± {results['cv_std']:.3f}")

        # Save model
        self.save_model()

        return results

    def predict(self, trade_data: Dict) -> Dict:
        """Predict outcome for a potential trade"""
        if self.model is None:
            return {'error': 'Model not trained'}

        # Create features
        X = self.feature_engineer.create_feature_vector(trade_data)

        # Ensure same columns as training
        for col in self.feature_columns:
            if col not in X.columns:
                X[col] = 0
        X = X[self.feature_columns]

        # Scale
        X_scaled = self.scaler.transform(X)

        # Predict
        prob = self.model.predict_proba(X_scaled)[0]

        return {
            'win_probability': float(prob[1]),
            'loss_probability': float(prob[0]),
            'recommendation': 'ENTER' if prob[1] > 0.6 else 'SKIP' if prob[1] < 0.4 else 'NEUTRAL',
            'confidence': 'HIGH' if abs(prob[1] - 0.5) > 0.3 else 'MEDIUM' if abs(prob[1] - 0.5) > 0.15 else 'LOW'
        }

    def get_feature_importance(self) -> pd.DataFrame:
        """Get feature importance ranking"""
        if self.model is None:
            return pd.DataFrame()

        if hasattr(self.model, 'feature_importances_'):
            importance = self.model.feature_importances_
        else:
            return pd.DataFrame()

        df = pd.DataFrame({
            'feature': self.feature_columns,
            'importance': importance
        }).sort_values('importance', ascending=False)

        return df

    def save_model(self):
        """Save model to disk"""
        data = {
            'model': self.model,
            'scaler': self.scaler,
            'feature_columns': self.feature_columns
        }
        with open(self.model_path, 'wb') as f:
            pickle.dump(data, f)
        print(f"   Model saved to {self.model_path}")

    def load_model(self):
        """Load model from disk"""
        try:
            with open(self.model_path, 'rb') as f:
                data = pickle.load(f)
            self.model = data['model']
            self.scaler = data['scaler']
            self.feature_columns = data['feature_columns']
            print(f"✓ Model loaded from {self.model_path}")
        except Exception as e:
            print(f"Could not load model: {e}")


# ============================================================================
# DATA REQUIREMENTS
# ============================================================================

DATA_REQUIREMENTS = """
╔══════════════════════════════════════════════════════════════════════════════╗
║                    ML SYSTEM DATA REQUIREMENTS                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  MINIMUM DATA FOR EACH MODEL TIER                                            ║
║  ─────────────────────────────────────────────────────────────────────────   ║
║                                                                              ║
║  ┌─────────────────┬────────────┬─────────────┬─────────────────────────┐   ║
║  │ Model Type      │ Min Trades │ Recommended │ Expected Accuracy       │   ║
║  ├─────────────────┼────────────┼─────────────┼─────────────────────────┤   ║
║  │ Rule-Based      │     0      │     50      │ N/A (handcrafted)       │   ║
║  │ Logistic Reg    │   100      │    300      │ 55-60%                  │   ║
║  │ Random Forest   │   200      │    500      │ 58-65%                  │   ║
║  │ XGBoost         │   300      │  1,000      │ 60-68%                  │   ║
║  │ Neural Network  │   500      │  2,000      │ 62-70%                  │   ║
║  │ LSTM/RNN        │ 1,000      │  5,000      │ 65-72%                  │   ║
║  │ Reinforcement   │ 2,000      │ 10,000      │ Varies (reward-based)   │   ║
║  └─────────────────┴────────────┴─────────────┴─────────────────────────┘   ║
║                                                                              ║
║  TIME TO COLLECT (at 20 trades/day)                                          ║
║  ─────────────────────────────────────────────────────────────────────────   ║
║                                                                              ║
║  • 100 trades  =  5 days                                                     ║
║  • 300 trades  = 15 days                                                     ║
║  • 500 trades  = 25 days (1 month)                                           ║
║  • 1,000 trades = 50 days (2 months)                                         ║
║  • 2,000 trades = 100 days (3-4 months)                                      ║
║  • 5,000 trades = 250 days (1 year)                                          ║
║                                                                              ║
║  FEATURE DATA REQUIREMENTS                                                   ║
║  ─────────────────────────────────────────────────────────────────────────   ║
║                                                                              ║
║  Per Trade (REQUIRED):                                                       ║
║  • Entry/Exit timestamps                                                     ║
║  • Entry/Exit Z-scores                                                       ║
║  • Entry/Exit spread values                                                  ║
║  • Direction (Long/Short Spread)                                             ║
║  • Lot size                                                                  ║
║  • Gross P&L, Costs, Net P&L                                                 ║
║  • Close reason                                                              ║
║                                                                              ║
║  Per Trade (RECOMMENDED):                                                    ║
║  • Hurst exponent at entry                                                   ║
║  • Half-life estimate                                                        ║
║  • Rolling volatility (1h, 4h, 24h)                                          ║
║  • Spread mean and std at entry                                              ║
║                                                                              ║
║  External Data (ADVANCED):                                                   ║
║  • Economic calendar events                                                  ║
║  • DXY correlation                                                           ║
║  • VIX level                                                                 ║
║  • COT positioning data                                                      ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""


# ============================================================================
# MAIN
# ============================================================================

def load_trades_from_db(db_path: str) -> List[Dict]:
    """Load trades from database"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        cursor.execute('SELECT * FROM trade_journal ORDER BY id')
        rows = cursor.fetchall()
        trades = [dict(row) for row in rows]
        return trades
    except Exception as e:
        print(f"Error loading trades: {e}")
        return []
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description='ML Trade Predictor')
    parser.add_argument('--train', action='store_true', help='Train model')
    parser.add_argument('--predict', action='store_true', help='Predict current conditions')
    parser.add_argument('--features', action='store_true', help='Show feature importance')
    parser.add_argument('--requirements', action='store_true', help='Show data requirements')
    parser.add_argument('--db', default='../basis_trading.db', help='Database path')
    parser.add_argument('--model', default='trade_predictor.pkl', help='Model path')

    args = parser.parse_args()

    if args.requirements:
        print(DATA_REQUIREMENTS)
        return

    if not HAS_ML:
        print("❌ ML packages not installed. Run:")
        print("   pip install pandas numpy scikit-learn xgboost")
        return

    predictor = TradePredictor(args.model)

    if args.train:
        # Load trades
        trades = load_trades_from_db(args.db)
        if not trades:
            print("No trades found in database")
            return

        print(f"Loaded {len(trades)} trades from database")

        # Train
        results = predictor.train(trades)

        # Show feature importance
        importance = predictor.get_feature_importance()
        print("\n📊 Top 10 Important Features:")
        print(importance.head(10).to_string(index=False))

    elif args.features:
        importance = predictor.get_feature_importance()
        if importance.empty:
            print("No model trained yet. Run --train first.")
        else:
            print("\n📊 Feature Importance Ranking:")
            print(importance.to_string(index=False))

    elif args.predict:
        # Example prediction
        sample_trade = {
            'entry_time': datetime.now(),
            'entry_zscore': -3.5,
            'entry_spread': 9.07,
            'mean': 8.50,
            'std': 0.30,
            'hurst': 0.42,
            'half_life': 35,
            'vol_1h': 0.015,
            'vol_4h': 0.012,
            'vol_24h': 0.010,
            'direction': 'Long Spread',
            'lot_size': 20,
            'cost_per_lot': 48
        }

        result = predictor.predict(sample_trade)

        print("\n📊 Trade Prediction:")
        print(f"   Win Probability: {result['win_probability']:.1%}")
        print(f"   Recommendation: {result['recommendation']}")
        print(f"   Confidence: {result['confidence']}")

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
