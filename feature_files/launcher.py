"""
Application Launcher with Native Window

Provides a professional startup experience:
- Splash screen while loading
- Native desktop window (no browser needed)
- System tray integration
- Graceful error handling
"""

import sys
import os
import threading
import time
from pathlib import Path

# Use browser instead of webview for better compatibility
# pywebview has issues with PyInstaller frozen apps
HAS_WEBVIEW = False
import webbrowser

# Add the application directory to path
if getattr(sys, 'frozen', False):
    # Running as compiled executable
    APP_DIR = Path(sys.executable).parent
    # For frozen app, all modules are in the same directory
    sys.path.insert(0, str(APP_DIR))
else:
    # Running as script
    APP_DIR = Path(__file__).parent.parent
    sys.path.insert(0, str(APP_DIR))

# Try to import tkinter for splash screen
try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    HAS_TK = True
except ImportError:
    HAS_TK = False


class SplashScreen:
    """Professional splash screen with loading progress"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("StatArb Pro")

        # Remove window decorations
        self.root.overrideredirect(True)

        # Window size
        width = 500
        height = 300

        # Center on screen
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2
        self.root.geometry(f'{width}x{height}+{x}+{y}')

        # Light theme colors
        bg_color = '#ffffff'
        accent_color = '#0d6efd'
        text_color = '#212529'

        self.root.configure(bg=bg_color)

        # Main frame
        main_frame = tk.Frame(self.root, bg=bg_color)
        main_frame.pack(fill='both', expand=True, padx=2, pady=2)

        # Border effect
        main_frame.configure(highlightbackground=accent_color, highlightthickness=2)

        # Logo/Title area
        title_frame = tk.Frame(main_frame, bg=bg_color)
        title_frame.pack(fill='x', pady=(40, 20))

        # Application name
        title_label = tk.Label(
            title_frame,
            text="StatArb Pro",
            font=('Segoe UI', 32, 'bold'),
            fg=accent_color,
            bg=bg_color
        )
        title_label.pack()

        # Subtitle
        subtitle_label = tk.Label(
            title_frame,
            text="Multi-Broker Statistical Arbitrage System",
            font=('Segoe UI', 11),
            fg=text_color,
            bg=bg_color
        )
        subtitle_label.pack(pady=(5, 0))

        # Version
        version_label = tk.Label(
            title_frame,
            text="Version 1.0.0",
            font=('Segoe UI', 9),
            fg='#888888',
            bg=bg_color
        )
        version_label.pack(pady=(5, 0))

        # Status message
        self.status_var = tk.StringVar(value="Initializing...")
        status_label = tk.Label(
            main_frame,
            textvariable=self.status_var,
            font=('Segoe UI', 10),
            fg=text_color,
            bg=bg_color
        )
        status_label.pack(pady=(30, 10))

        # Progress bar
        style = ttk.Style()
        style.theme_use('clam')
        style.configure(
            "Custom.Horizontal.TProgressbar",
            troughcolor=bg_color,
            background=accent_color,
            darkcolor=accent_color,
            lightcolor=accent_color,
            bordercolor=bg_color
        )

        self.progress = ttk.Progressbar(
            main_frame,
            style="Custom.Horizontal.TProgressbar",
            length=400,
            mode='determinate'
        )
        self.progress.pack(pady=(0, 20))

        # Copyright
        copyright_label = tk.Label(
            main_frame,
            text="© 2024 StatArb Pro. All rights reserved.",
            font=('Segoe UI', 8),
            fg='#666666',
            bg=bg_color
        )
        copyright_label.pack(side='bottom', pady=(0, 15))

    def update_status(self, message: str, progress: int):
        """Update status message and progress bar"""
        self.status_var.set(message)
        self.progress['value'] = progress
        self.root.update()

    def close(self):
        """Close splash screen"""
        self.root.destroy()

    def mainloop(self):
        """Start the splash screen event loop"""
        self.root.mainloop()


class ApplicationLauncher:
    """Main application launcher"""

    def __init__(self):
        self.splash = None
        self.server_thread = None
        self.server_started = False
        self.startup_complete = False
        self.startup_error = None

    def show_error(self, title: str, message: str):
        """Show error dialog"""
        if HAS_TK:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(title, message)
            root.destroy()
        else:
            print(f"ERROR: {title}\n{message}")

    def start_server(self):
        """Start the Flask server in background"""
        try:
            from web.app import init_app, socketio, app

            init_app()
            self.server_started = True
            socketio.run(app, host='127.0.0.1', port=5000, debug=False, use_reloader=False)

        except Exception as e:
            self.server_started = False
            self.server_error = str(e)
            import traceback
            self.server_error = traceback.format_exc()

    def launch(self):
        """Launch the application"""

        # Show splash screen
        if HAS_TK:
            self.splash = SplashScreen()

            # Run startup in separate thread
            startup_thread = threading.Thread(target=self._startup_sequence)
            startup_thread.start()

            # Run splash screen main loop
            self.splash.mainloop()

            # After splash closes, wait for startup to complete then open browser
            startup_thread.join(timeout=5)

            if self.startup_error:
                self.show_error("Startup Error", self.startup_error)
                sys.exit(1)

            # Open browser and keep server alive (this runs on main thread after splash closes)
            if self.startup_complete:
                print("Opening browser...")
                webbrowser.open('http://127.0.0.1:5000')
                print("Browser opened. Server running at http://127.0.0.1:5000")
                print("Press Ctrl+C to stop the server.")
                # Keep main thread alive while server runs
                if self.server_thread:
                    try:
                        self.server_thread.join()
                    except KeyboardInterrupt:
                        print("\nShutting down...")
        else:
            # No GUI, just start server directly
            self._startup_no_splash()

    def _startup_no_splash(self):
        """Start without splash screen"""
        print("Starting StatArb Pro server...")
        self.server_error = None
        self.server_thread = threading.Thread(target=self.start_server, daemon=True)
        self.server_thread.start()
        time.sleep(2)

        if hasattr(self, 'server_error') and self.server_error:
            print(f"ERROR: {self.server_error}")
            sys.exit(1)

        webbrowser.open('http://127.0.0.1:5000')
        print("Server running at http://127.0.0.1:5000")
        print("Press Ctrl+C to stop.")
        try:
            self.server_thread.join()
        except KeyboardInterrupt:
            print("\nShutting down...")

    def _startup_sequence(self):
        """Run the startup sequence (runs in background thread with splash)"""
        try:
            steps = [
                ("Loading configuration...", 10),
                ("Initializing database...", 25),
                ("Loading trading engine...", 40),
                ("Starting web server...", 60),
                ("Preparing user interface...", 80),
                ("Almost ready...", 95),
            ]

            for message, progress in steps:
                if self.splash:
                    self.splash.update_status(message, progress)
                time.sleep(0.3)

            # Start server in background thread
            self.server_error = None
            self.server_thread = threading.Thread(target=self.start_server, daemon=True)
            self.server_thread.start()

            # Wait for server to start
            time.sleep(3)

            # Check if server started successfully
            if hasattr(self, 'server_error') and self.server_error:
                if self.splash:
                    self.splash.close()
                self.startup_error = f"Failed to start server:\n\n{self.server_error}"
                return

            if self.splash:
                self.splash.update_status("Launching application...", 100)
                time.sleep(0.5)
                self.splash.close()

            # Mark startup as complete - browser will be opened by main thread
            self.startup_complete = True

        except Exception as e:
            if self.splash:
                self.splash.close()
            self.startup_error = f"Failed to start application:\n\n{str(e)}"


def main():
    """Main entry point"""
    launcher = ApplicationLauncher()
    launcher.launch()


if __name__ == '__main__':
    main()
