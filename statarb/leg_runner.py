"""Leg runner: one process per MT5 account.

Connects to its own terminal (via path/login from config) and serves
tick + order requests to the coordinator over localhost TCP. Run one
of these per account:

    python run_leg.py --config config.json --account account_a
    python run_leg.py --config config.json --account account_b

The runner stays up across coordinator restarts; stop it with Ctrl+C.
"""

import argparse
import logging
import socket
import sys

from .broker import BrokerSession
from .config import AlgoTradingConfig
from .ipc import JsonLineSocket, parse_endpoint
from .legs import LocalLeg


class LegServer:
    def __init__(self, broker, host='127.0.0.1', port=0):
        self.leg = LocalLeg(broker)
        self._stop = False
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((host, port))
        self.server.listen(1)
        self.host, self.port = self.server.getsockname()

    def handle(self, msg):
        cmd = msg.get('cmd')
        try:
            if cmd == 'ping':
                return {'ok': True, 'account': self.leg.name}
            if cmd == 'account_info':
                account = self.leg.account_info()
                return {'ok': account is not None, 'account': account}
            if cmd == 'ensure_symbol':
                return self.leg.ensure_symbol(msg['symbol'])
            if cmd == 'tick':
                tick = self.leg.tick(msg['symbol'])
                if tick is None:
                    return {'ok': False, 'error': f"No tick for {msg['symbol']}"}
                return {'ok': True, 'tick': tick}
            if cmd == 'order':
                return self.leg.order(
                    msg['symbol'], msg['side'], msg['volume'],
                    slippage_points=msg.get('slippage_points', 1.0),
                    comment=msg.get('comment', ''))
            if cmd == 'place_limit':
                return self.leg.place_limit(
                    msg['symbol'], msg['side'], msg['volume'], msg['price'],
                    comment=msg.get('comment', ''),
                    position_ticket=msg.get('position_ticket'))
            if cmd == 'pending_orders':
                return {'ok': True,
                        'orders': self.leg.pending_orders(msg.get('symbol'))}
            if cmd == 'modify_order':
                return self.leg.modify_order(msg['ticket'], msg['price'])
            if cmd == 'cancel_order':
                return self.leg.cancel_order(msg['ticket'])
            if cmd == 'order_state':
                return self.leg.order_state(msg['ticket'])
            if cmd == 'positions':
                return {'ok': True,
                        'positions': self.leg.positions(msg.get('symbol'))}
            if cmd == 'order_log':
                return {'ok': True,
                        'orders': self.leg.order_log(msg.get('hours', 24))}
            if cmd == 'terminal_report':
                return {'ok': True, 'report': self.leg.terminal_report()}
            if cmd == 'symbol_report':
                return {'ok': True,
                        'report': self.leg.symbol_report(msg['symbol'])}
            if cmd == 'find_symbols':
                return {'ok': True,
                        'symbols': self.leg.find_symbols(
                            msg.get('pattern', ''), msg.get('limit', 40))}
            if cmd == 'close_ticket':
                return self.leg.close_ticket(
                    msg['symbol'], msg['ticket'], msg['volume'],
                    msg['entry_side'],
                    slippage_points=msg.get('slippage_points', 1.0),
                    comment=msg.get('comment', ''))
            return {'ok': False, 'error': f'Unknown command: {cmd}'}
        except Exception as e:
            logging.error("Error handling %s: %s", cmd, e)
            return {'ok': False, 'error': str(e)}

    def serve_forever(self):
        logging.info("Leg runner '%s' listening on %s:%s",
                     self.leg.name, self.host, self.port)
        while not self._stop:
            try:
                conn, addr = self.server.accept()
            except OSError:
                break  # socket closed by stop()
            logging.info("Coordinator connected from %s", addr)
            js = JsonLineSocket(conn)
            try:
                while not self._stop:
                    msg = js.recv()
                    if msg is None:
                        break
                    js.send(self.handle(msg))
            except (OSError, ValueError) as e:
                logging.warning("Coordinator connection dropped: %s", e)
            finally:
                js.close()
                logging.info("Coordinator disconnected; waiting for next connection")

    def stop(self):
        self._stop = True
        try:
            self.server.close()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description="MT5 leg runner (one per account)")
    parser.add_argument('--config', required=True, help='Path to config JSON')
    parser.add_argument('--account', required=True,
                        help='Account name from the config accounts section')
    parser.add_argument('--listen', default=None,
                        help='host:port override (default: account endpoint)')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - [leg] %(message)s',
        handlers=[
            logging.FileHandler(f'leg_{args.account}.log', encoding='utf-8'),
            logging.StreamHandler(),
        ],
    )

    config = AlgoTradingConfig.from_file(args.config)
    if args.account not in config.accounts:
        print(f"Unknown account '{args.account}'. "
              f"Available: {list(config.accounts)}")
        sys.exit(1)

    account = config.accounts[args.account]
    endpoint = args.listen or account.endpoint
    if not endpoint:
        print(f"Account '{args.account}' has no endpoint in config and no "
              f"--listen given (e.g. --listen 127.0.0.1:9101)")
        sys.exit(1)
    host, port = parse_endpoint(endpoint)

    broker = BrokerSession(account)
    if not broker.initialize():
        print(f"Failed to connect account '{args.account}' to MT5 — "
              f"check terminal_path/login/server/.env")
        sys.exit(1)

    server = LegServer(broker, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nLeg runner stopped")
    finally:
        server.stop()
        broker.shutdown()


if __name__ == '__main__':
    main()
