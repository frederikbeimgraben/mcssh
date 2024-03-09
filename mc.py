#!/usr/bin/env python3
"""
WebSocket Connection to get log data from the server
- ws://<host>:4567/v1/ws/console

To authenticate we use a cookie with the name "x-servertap-key"
"""

import json
import re
import time
from typing import Callable, Dict, List, Tuple
import os
import datetime
# Import SocketIO
import requests
import websocket

import server

from threading import Thread

KNOWN_COMMANDS = []

if not os.path.exists(".messages"):
    with open(".messages", "w") as f:
        f.write("[]")

with open(".messages", "r") as f:
    KNOWN_MESSAGES: List[Dict] = json.load(f)
 
# If the file does not exist, create it
if not os.path.exists("commands.txt"):
    with open("commands.txt", "w") as f:
        f.write("")

with open("commands.txt", "r") as f:
    KNOWN_COMMANDS = f.read().split("\n")

def getToken() -> str:
    """
    Try to get token from either environment variable or from the file
    .sec
    """
    
    # Try to get the token from the environment variable
    token = os.environ.get("MCSSH_SECRET")
    
    if token is not None:
        return token
    
    # Try to get the token from the file
    try:
        with open(".sec", "r") as f:
            token = f.read()
            return token
    except FileNotFoundError:
        pass

class MinecraftSocket:
    ws: websocket.WebSocketApp
    host: str
    
    latest_message: int = 0
    
    callbacks: List['server.SSHServer']
    
    _players: List[str]
    players_last_updated: int = 0
    
    @property
    def known_commands(self) -> List[str]:
        return KNOWN_COMMANDS
    
    def __init__(self, host: str):
        self.host = host
        self.callbacks = []
        
    def get_online_players(self) -> List[str]:
        """
        Get the online players
        """
        
        if time.time() - self.players_last_updated < 10:
            return self._players
        
        self.players_last_updated = time.time()
        
        url = f"http://{self.host}:4567/v1/players"
        
        try:
            response = requests.get(url, headers={"key": getToken()})
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            server.log(f"Error getting online players: {e}")
            return []
        
        self._players = response.json()
        
        return response.json()
    
    @property
    def players(self) -> List[str]:
        return [player["displayName"] for player in self.get_online_players()]
            
    def format_message(self, message: Dict) -> str:
        time = message["timestampMillis"]
        time_formatted = datetime.datetime.fromtimestamp(time / 1000).strftime('%Y-%m-%d %H:%M:%S')
        
        return f"{time_formatted} {message['level']} : {message['message']}"
    
    def subscribe(self, callbackServer: 'server.SSHServer'):
        server.log(f"[WebSocket] Subscribed by {callbackServer}")
        
        self.callbacks.append(callbackServer)
        
        server.log(f"[WebSocket] Callbacks: {self.callbacks}")
        
    def unsubscribe(self, callbackServer: 'server.SSHServer'):
        server.log(f"[WebSocket] Unsubscribed by {callbackServer}")
        
        self.callbacks.remove(callbackServer)
        
        server.log(f"[WebSocket] Callbacks: {self.callbacks}")
        
    def on_open(self, ws):
        server.log("[WebSocket] Opened new connection")
        
    def on_message(self, ws, message, *args, **kwargs):
        global KNOWN_MESSAGES
        
        json_message = json.loads(message)
        
        # If the message is older than the latest message, ignore it
        if json_message["timestampMillis"] < self.latest_message:
            return
        
        if message in KNOWN_MESSAGES:
            return
        
        KNOWN_MESSAGES.append(message)
        
        self.latest_message = json_message["timestampMillis"]
        
        with open(".messages", "w") as f:
            json.dump(KNOWN_MESSAGES, f, indent=4)
        
        server.log(f"[WebSocket] Received message: {message}")
        
        # If the message starts with /<...>, then it is a command
        # Add to KNOWN_COMMANDS and dont emit
        global KNOWN_COMMANDS
        
        regex = r"^\/([^\s]+):"
        
        if (match := re.match(regex, json_message["message"])):
            command = match.group(1)
            
            if command not in KNOWN_COMMANDS:
                KNOWN_COMMANDS.append(command)
                with open("commands.txt", "w") as f:
                    f.write("\n".join(KNOWN_COMMANDS))
            
        try:
            formatted = self.format_message(json_message)
        except Exception as e:
            server.log(f"[WebSocket] Error formatting message: {e}")
            return
        
        for callback in self.callbacks:
            def wrapper():
                try:
                    with callback.lock:
                        callback.mc_callback(ws, formatted)
                except Exception as e:
                    server.log(f"[WebSocket] Error in callback: {e}")
                
                    # Remove the callback
                    self.callbacks.remove(callback)
                
            Thread(target=wrapper).start()
            
    def send(self, message: str):
        try:
            self.ws.send(message)
        except BrokenPipeError:
            self.start()
    
    def on_close(self, ws, *args, **kwargs):
        server.log("[WebSocket] Closed connection, restarting...")
        
        self.start()
        
    def on_error(self, ws, error, *args, **kwargs):
        server.log(f"[WebSocket] Error: {error}")
        
    def is_valid_command(self, command: str) -> bool:
        return command in KNOWN_COMMANDS
    
    def start(self):
        # Get the token
        token = getToken()
        
        # Set the cookie
        cookie = f"x-servertap-key={token}"
        
        # Define the URL
        url = f"ws://{self.host}:4567/v1/ws/console"
        
        headers = {
            "Cookie": cookie
        }
        
        # Create the WebSocket
        server.log(f"[WebSocket] Connecting to {url}", end=": ")
        self.ws = websocket.WebSocketApp(url, on_message=self.on_message, on_open=self.on_open, header=headers, on_close=self.on_close, on_error=self.on_error)
        
        # Create a thread to run the WebSocket
        wsThread = Thread(target=self.ws.run_forever)
        wsThread.start()
        
        # Wait for the WebSocket to open
        i = 0
        while not self.ws.sock or not self.ws.sock.connected:
            if i % 10 == 0:
                server.log(".")
            i += 1
            time.sleep(0.1)
            
        server.log("[WebSocket] Connected")