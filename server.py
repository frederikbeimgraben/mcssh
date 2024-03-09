#!/usr/bin/env python3
"""
Paramiko Server for SSH (Minecraft console via SSH)
"""

# Import the required libraries
import datetime
import os
import threading
from typing import Any, Generator, List
import paramiko
import socket
from sshpubkeys import SSHKey
from subprocess import Popen

# Local imports
import mc

# Set logging level
import logging
logging.basicConfig(level=logging.INFO)

THREADS: List[threading.Thread] = []

def log(data: str, end: str="\n"):
    """
    Log data to the file
    """
    
    timestamp = str(datetime.datetime.now())
    
    string = f"[{timestamp}] {data}{end}"
    
    with open("server_log.txt", "a") as f:
        f.write(string)
    
    print(string, end="")

# Read authorized keys from .authorized_keys
def getAuthorizedKeys() -> Generator[paramiko.PKey, None, None]:
    """
    Read all .pub files from the folder ./authorized_keys
    """
    
    # If the folder does not exist, create it
    if not os.path.exists("./authorized_keys"):
        os.mkdir("./authorized_keys")
    
    # Get all the files in the folder
    files = os.listdir("./authorized_keys")
    
    log(f"[*] Reading {len(files)} public keys")
    
    # Read the public keys
    for file in files:
        with open(f"./authorized_keys/{file}", "r") as f:
            key: SSHKey = SSHKey(f.read())
            
            # Convert to paramiko key
            yield paramiko.RSAKey(key=key.rsa)
    
# Create a new class for the SSH server
class SSHServer(paramiko.ServerInterface):
    buffer: List[str] = None
    history: List[str] = None
    position: int = 0
    selected: int = 0
    selected_suffix: int = 0
    filter: str = ""
    
    input_thread: threading.Thread = None
    closing: bool = False
    
    _lock: threading.Lock = None
    
    @property
    def lock(self):
        # Log (for thread safety)
        # Return an object that can be entered and exited
        if self._lock is None:
            self._lock = threading.Lock()
            
        return self._lock
    
    @property
    def player_suggestions(self) -> List[str]:
        words = self.filter.split(" ")
        
        if len(self.filter) == 0 or len(words[0]) == 0:
            return []
        
        if len(words) == 1 and self.is_command_complete:
            return self.ws.players
        
        return [
            self.buffer_str + player[len(words[-1]):]
            for player in self.ws.players if player.startswith(words[-1])
        ]
    
    @property
    def selection(self) -> str:
        if len(self.filtered_history) == 0:
            return self.filter
        
        if self.selected >= len(self.filtered_history):
            self.selected = 0
        
        return self.filtered_history[self.selected]
    
    @property
    def suffix_selection(self) -> str:
        if len(self.filtered_history[1:]) == 0:
            return ""
        
        if self.selected_suffix >= len(self.filtered_history[1:]):
            self.selected_suffix = 0
        
        return self.filtered_history[1:][self.selected_suffix]
    
    def __init__(self, ws: Any = None):
        self.ws = ws
        self.buffer = []
        self.history = [""]
        
        ws.subscribe(self)
        
    def check_channel_request(self, kind, chanid):
        if kind == "session":
            self.channel = paramiko.Channel(chanid)
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
    
    def check_auth_password(self, username, password):
        return paramiko.AUTH_FAILED
    
    def check_auth_publickey(self, username, key):        
        authorizedKeys = getAuthorizedKeys()
        
        # Check if SHA256 of the key is in the authorized keys
        for authorizedKey in authorizedKeys:
            if key.get_fingerprint() == authorizedKey.get_fingerprint():
                log(f"[!] Accepted public key: ...{key.get_base64()[-8:]}")
                return paramiko.AUTH_SUCCESSFUL
            
        return paramiko.AUTH_FAILED
    
    def get_allowed_auths(self, username):
        return "publickey"
    
    def check_channel_pty_request(self, channel: paramiko.Channel, term: bytes, width: int, height: int, pixelwidth: int, pixelheight: int, modes: bytes) -> bool:
        self.channel = channel
        self.width = width
        self.height = height
        
        # Start thread
        self.input_thread = threading.Thread(target=self.input_handler)
        self.input_thread.start()
        
        global THREADS
        THREADS.append(self.input_thread)
        
        return True
    
    def check_channel_window_change_request(self, channel: paramiko.Channel, width: int, height: int, pixelwidth: int, pixelheight: int) -> bool:
        self.width = width
        self.height = height
        return True
    
    def check_channel_shell_request(self, channel: paramiko.Channel) -> bool:
        self.channel = channel
        
        return True
    
    def check_channel_exec_request(self, channel: paramiko.Channel, command: str) -> bool:
        return True
    
    def get_server():
        return "SSH-2.0-OpenSSH_7.6p1 Ubuntu-4ubuntu0.3"
    
    def add_history(self, cmd: str):
        if len(self.history) == 1 or self.history[1] != cmd:
            self.history.insert(0, cmd)
            
        # Save the history to a file
        with open("history.txt", "w") as f:
            f.write("\n".join(self.history[1:]))
    
    def load_history(self):
        try:
            with open("history.txt", "r") as f:
                self.history = f.read().split("\n")
        except FileNotFoundError:
            pass
    
    def mc_callback(self, ws: Any, data: str):
        """
        Callback function to send data to the SSH client
        """
        
        width = self.width
        
        while len(data) > 0:
            pos: int = 0
            chunk: str = data[:self.width]
            
            # Set cursor to the beginning of the line
            self.send_to_client("\r")
            
            self.send_to_client(' ' * (self.width - width), True)
            
            # Send the chunk
            self.send_to_client(chunk, True)
            
            # Remove the chunk from the data
            data = data[self.width:]
            
            # Send a newline
            self.send_to_client("\n", True)
            
            self.send_to_client("\r")
            
            width = self.width - 27
            
            if width < 0:
                width = self.width
        
        # Restore the prompt
        self.redraw_buffer()
    
    def redraw_buffer(self):
        self.send_to_client("\r")
        self.send_to_client("\033[J")
        self.send_to_client(self.buffer_str)
    
    def input_handler(self):
        while not self.closing:
            try:
                data = self.channel.recv(1024).decode()
                if not data:
                    break
                self.buffer_str += data
                self.redraw_buffer()
            except Exception as e:
                log(f'[!] Error while receiving data: {e}')
                break
    
    def send_to_client(self, data: str, server: bool=False):
        """
        Send data to the SSH client
        """
        
        # log(f"[*] Sending to client: {data.encode()}")
        
        try:
            # Send the data
            self.channel.send(data.encode())
        except OSError:
            pass
        
        if server:
            with open("log.txt", "ab") as f:
                f.write(data.encode())
        
    def mc_callback(self, ws: Any, data: str):
        """
        Callback function to send data to the SSH client
        """
        
        log(f"[*] Sending to client: {data}")
        
        width = self.width
        
        while len(data) > 0:
            chunk: str = data[:self.width]
            
            # Set cursor to the beginning of the line
            self.send_to_client("\r")
            
            self.send_to_client(' ' * (self.width - width), True)
            
            # Send the chunk
            self.send_to_client(chunk, True)
            
            # Remove the chunk from the data
            data = data[self.width:]
            
            # Send a newline
            self.send_to_client("\n", True)
            
            self.send_to_client("\r")
            
            width = self.width - 27
            
            if width < 0:
                width = self.width
        
        # Restore the prompt
        self.redraw_buffer()
    
    def check_channel_exec_request(self, channel: paramiko.Channel, command: str) -> bool:
        return True
    
    def get_server():
        return "SSH-2.0-OpenSSH_7.6p1 Ubuntu-4ubuntu0.3"
    
    def add_history(self, cmd: str):
        if len(self.history) == 1 or self.history[1] != cmd:
            self.history.insert(0, cmd)
            
        # Save the history to a file
        with open("history.txt", "w") as f:
            f.write("\n".join(self.history[1:]))
    
    def load_history(self):
        try:
            with open("history.txt", "r") as f:
                self.history = f.read().split("\n")
        except FileNotFoundError:
            pass
    
    @property
    def player_suggestions(self) -> List[str]:
        words = self.filter.split(" ")
        
        if len(self.filter) == 0 or len(words[0]) == 0:
            return []
        
        if len(words) == 1 and self.is_command_complete:
            return self.ws.players
        
        return [
            self.buffer_str + player[len(words[-1]):]
            for player in self.ws.players if player.startswith(words[-1])
        ]
    
    @property
    def selection(self) -> str:
        if len(self.filtered_history) == 0:
            return self.filter
        
        if self.selected >= len(self.filtered_history):
            self.selected = 0
        
        return self.filtered_history[self.selected]
    
    @property
    def suffix_selection(self) -> str:
        if len(self.filtered_history[1:]) == 0:
            return ""
        
        if self.selected_suffix >= len(self.filtered_history):
            self.selected_suffix = 0
        
        return self.filtered_history[1:][self.selected_suffix]
    
    @property
    def suffix(self) -> str:
        if len(self.filter) == 0:
            return ""
        
        return ''.join(list(self.suffix_selection)[len(self.filter):])
    
    @property
    def buffer_str(self) -> str:
        return "".join(self.buffer)
    
    @property
    def is_command_complete(self) -> bool:
        return self.buffer_str.strip().removeprefix('/') in self.ws.known_commands
    
    @property
    def buffer_formatted(self):
        """
        Format using ANSI
        """
        
        # If the buffer is empty, return
        if len(self.buffer) == 0:
            return ""
        
        # Split the buffer into parts
        parts = ''.join(self.buffer).split(" ")
        
        # If the first word is a command, format it light blue and bold
        if len(parts[0]) > 0 and parts[0][0].strip() == "!":
            parts[0] = f"\x1b[1;36m{parts[0]}"
            parts[-1] = f"{parts[-1]}\x1b[0m"
        elif parts[0].strip() in ["exit", "clear", "reload", "cls", "reset"]:
            parts[0] = f"\x1b[1;34m{parts[0]}\x1b[0m"
        elif (
                self.ws.is_valid_command(parts[0]) or 
                (self.ws.is_valid_command(parts[0][1:]) and parts[0][0] == "!")
            ):
            parts[0] = f"\x1b[1;32m{parts[0]}\x1b[0m"
        else:
            parts[0] = f"\x1b[1;31m{parts[0]}\x1b[0m"
            
        # Join the parts
        return " ".join(parts)
    
    def redraw_buffer(self):
        # Clear the line
        self.send_to_client("\x1b[2K")
        
        # Print the buffer
        self.send_to_client(f"\x1b[{self.height};1H> {self.buffer_formatted}")
        
        # Print completion
        self.send_to_client(f"\x1b[{self.height};{len(self.buffer_str) + 3}H")
        
        # Light gray completion
        self.send_to_client(f"\x1b[30m{self.suffix}\x1b[0m")
        
        # Move the cursor to the right position
        self.send_to_client(f"\x1b[{self.height};{self.position+3}H")
        
    def accept_completion(self):
        self.buffer = list(self.suffix_selection)
        self.filter = self.buffer_str
        self.position = len(self.buffer)
        self.redraw_buffer()
        
    def close(self):
        self.channel.close()
        self.closing = True
        
        try:
            self.input_thread.join()
        except RuntimeError:
            pass
        
        self.ws.unsubscribe(self)
    
        log(f"[*] Closed connection to {self.channel.getpeername()}")
        
    def send_command(self):
        if len(self.buffer) == 0:
            return
        
        self.add_history("".join(self.buffer))
        
        if ''.join(self.buffer) == "reload":
            self.buffer = list("reload confirm")
        
        if ''.join(self.buffer) == "exit":
            self.close()
            exit(0)
        elif ''.join(self.buffer) in ["clear", "cls"]:
            self.send_to_client("\x1b[2J")
            self.buffer = []
            self.position = 0
            
            with open("log.txt", "wb") as f:
                f.write(b"")
            
            log(f"[*] Cleared log")
        elif ''.join(self.buffer) == "reset":
            # Restart this server
            # Popen sudo systemctl restart mcssh
            Popen(["sudo", "systemctl", "restart", "mcssh.service"])
            
            log(f"[*] Restarting server")            
        elif "".join(self.buffer).strip()[0] == "!":
            # Send broadcast
            self.ws.send("/broadcast " + "".join(self.buffer[1:]))
            
            log(f"[*] Sent broadcast: {''.join(self.buffer[1:])}")
        else:
            self.ws.send("".join(self.buffer))
            
            log(f"[*] Sent command: {''.join(self.buffer)}")
        
        self.buffer = []
        self.position = 0
        self.selected = 0
        self.selected_suffix = 0
        
        # clear the line
        self.send_to_client("\x1b[2K")
        
    @property
    def filtered_history(self):
        filtered = [cmd for cmd in (self.player_suggestions + self.history + self.ws.known_commands) if cmd.startswith(self.filter)]
        
        # Remove duplicates that are next to each other
        return [self.filter] + [filtered[i] for i in range(len(filtered)) if i == 0 or filtered[i] != filtered[i-1]]
        
    def previous_command(self):
        if self.filter == "":
            self.selected = min(len(self.filtered_history) - 1, self.selected + 1)
            self.buffer = list(self.filtered_history[self.selected])
            self.position = len(self.buffer)
        else:
            self.selected_suffix = min(len(self.filtered_history) - 1, self.selected_suffix + 1)
        
    def next_command(self):
        if self.selected == 0 and self.filter == "":
            self.buffer = []
            self.position = 0
            return
        
        if self.filter == "":
            self.selected = max(0, self.selected - 1)
            
            self.buffer = list(self.filtered_history[self.selected])
            self.position = len(self.buffer)
        else:
            self.selected_suffix = max(0, self.selected_suffix - 1)
        
    def backspace(self):
        if len(self.buffer) > 0:
            self.buffer.pop(self.position - 1)
            self.position -= 1
    
    def update_filter(self):
        self.filter = self.buffer_str
    
    def input_handler(self):        
        self.buffer = []
        self.position = 0
        self.selected = 0
        
        self.load_history()
        
        # Clear the screen
        self.send_to_client("\x1b[2J")
        
        # with open("log.txt", "rb") as f:
        #    log = f.read().decode("utf-8").split("\n")
            
        # Align at bottom
        # y = self.height - min(len(log), self.height)
        # self.send_to_client(f"\x1b[{y};1H")
        
        # Send first self.height-1 lines of LOG
        # self.send_to_client("\n\r".join(log[-(self.height-1):]))
        
        # Send initial prompt
        self.send_to_client(f"\x1b[{self.height};1H> ")
        
        while True:
            # Receive
            while True:
                data = self.channel.recv(1)
                
                try:
                    # Decode utf-8
                    data.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    continue
            
            # If the data is an escape sequence, receive until the end
            if data == b"\x1b":
                while True:
                    data += self.channel.recv(1)
                    if data[-1] in b"ABCDEFGHJKSTfmnsulh~":
                        break
            
            match data:
                # Return
                case b"\r":                    
                    self.send_command()
                    self.update_filter()
                # Backspace
                case b"\x7f":
                    self.backspace()
                    self.update_filter()
                # Keyboad interrupt
                case b"\x03":
                    if len(self.buffer) > 0:
                        self.buffer = []
                        self.position = 0
                    else:
                        self.close()
                        exit(0)
                # Up arrow (history)
                case b"\x1b[A":
                    self.previous_command()
                # Down arrow (history)
                case b"\x1b[B":
                    self.next_command()
                # Left arrow
                case b"\x1b[D":
                    if self.position > 0:
                        self.position -= 1
                # Right arrow
                case b"\x1b[C":
                    if self.position == len(self.buffer):
                        self.accept_completion()
                        self.update_filter()
                    elif self.position < len(self.buffer):
                        self.position += 1
                # Tab
                case b"\t":
                    self.accept_completion()
                    self.update_filter()
                # Delete
                case b"\x1b[3~":
                    if self.position < len(self.buffer):
                        self.buffer.pop(self.position)
                    self.update_filter()
                case _:
                    self.buffer.insert(self.position, data.decode("utf-8"))
                    self.position += 1
                    self.update_filter()
                    
            self.redraw_buffer()
        
def get_server_key():
    """
    Get the server key
    """
    try:
        return paramiko.RSAKey(filename="server.key")
    except FileNotFoundError:
        key = paramiko.RSAKey.generate(2048)
        with open("server.key", "wb") as f:
            key.write_private_key(f)
        return key

def main():
    key = get_server_key()
    
    ws = mc.MinecraftSocket("10.66.66.111")
    
    ws.start()
    
    # Create the socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", 2200))

    # Start listening for connections
    sock.listen(100)
    log("[*] Listening for connection ...")
    
    while True:
        try:
            client, addr = sock.accept()
            
            # Check if a connection was received
            if not client or not addr:
                continue
            
            log(f"[*] Accepted connection from {addr[0]}:{addr[1]}")
            
            server = SSHServer(ws)
            
            # Create the transport
            transport = paramiko.Transport(client)
            transport.add_server_key(key)
            
            # Start the server
            transport.start_server(server=server)
        except KeyboardInterrupt:
            log("[!] Stopping server ...")
            
            for thread in THREADS:
                thread.join()
            
            break
        except Exception as e:
            log(f"[!] Error: {e}")
            continue
        
    
if __name__ == "__main__":
    main()