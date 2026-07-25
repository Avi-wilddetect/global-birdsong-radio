# FILE: proxy_manager.py
# VERSION: 3.0 - "The Self-Healing & Socket Linger Patch"
# PURPOSE: Handles both HTTPS (CONNECT) and HTTP (GET/POST) Proxying.
# FIXED: 
# 1. Reduced buffer to 32KB to prevent USB Wi-Fi driver crashes.
# 2. Changed to dynamic Interface Name binding to auto-heal if an adapter restarts.
# 3. Added SO_LINGER to aggressively destroy dead sockets, preventing Windows TCP exhaustion.

import socket
import select
import threading
import logging
import psutil
import struct

# Configure simple logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [PROXY] - %(message)s')

def get_ip_for_interface(interface_name):
    """Dynamically fetches the current live IP address for a given Windows interface name."""
    if interface_name == "Default / OS" or not interface_name:
        return None
        
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        
        if interface_name in addrs:
            # Verify the interface is actually up
            if interface_name in stats and not stats[interface_name].isup:
                return None
                
            for snic in addrs[interface_name]:
                if snic.family == socket.AF_INET:
                    return snic.address
    except Exception as e:
        logging.error(f"[PROXY] Failed to get IP for {interface_name}: {e}")
        
    return None

class InterfaceProxy(threading.Thread):
    def __init__(self, interface_name, listen_port):
        super().__init__()
        self.interface_name = interface_name
        self.listen_port = listen_port
        self.running = True
        self.server_socket = None
        self.daemon = True

    def run(self):
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind(('127.0.0.1', self.listen_port))
            self.server_socket.listen(20)
            
            logging.info(f"Started Self-Healing Proxy on port {self.listen_port} bound to interface '{self.interface_name}'")
            
            while self.running:
                try:
                    client_socket, _ = self.server_socket.accept()
                    t = threading.Thread(target=self.handle_client, args=(client_socket,))
                    t.daemon = True
                    t.start()
                except OSError: break
        except Exception as e:
            logging.error(f"Server Error on {self.listen_port}: {e}")

    def stop(self):
        self.running = False
        if self.server_socket: self.server_socket.close()

    def handle_client(self, client_socket):
        remote_socket = None
        try:
            # TCP Exhaustion Fix: Aggressively destroy socket on close (skip TIME_WAIT)
            linger_enabled = struct.pack('ii', 1, 0)
            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger_enabled)
            
            request = b""
            client_socket.settimeout(5.0) # Safety timeout for headers
            
            # Read Headers
            while True:
                data = client_socket.recv(4096)
                if not data: break
                request += data
                if b"\r\n\r\n" in request: break
            
            if not request:
                client_socket.close()
                return

            first_line = request.split(b"\n")[0].decode('utf-8', 'ignore')
            
            # --- PARSE TARGET ---
            target_host = ""
            target_port = 80
            
            if 'CONNECT' in first_line:
                # HTTPS Tunnel: "CONNECT www.google.com:443 HTTP/1.1"
                target = first_line.split(' ')[1]
                target_host, target_port = target.split(':')
                target_port = int(target_port)
                is_https = True
            else:
                # Standard HTTP: "GET http://www.example.com/stream.m3u8 HTTP/1.1"
                is_https = False
                for line in request.split(b"\r\n"):
                    if line.lower().startswith(b"host:"):
                        host_part = line.split(b":", 1)[1].strip().decode('utf-8')
                        if ":" in host_part:
                            target_host, target_port = host_part.split(":")
                            target_port = int(target_port)
                        else:
                            target_host = host_part
                            target_port = 80
                        break
            
            if not target_host:
                client_socket.close()
                return

            # --- DYNAMIC BIND AND CONNECT ---
            remote_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            remote_socket.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger_enabled)
            
            if self.interface_name and self.interface_name != "Default / OS":
                # FETCH FRESH IP RIGHT BEFORE BINDING
                current_ip = get_ip_for_interface(self.interface_name)
                if not current_ip:
                    client_socket.close()
                    return
                    
                remote_socket.bind((current_ip, 0)) # BIND TO LIVE IP
            
            remote_socket.connect((target_host, target_port))

            # --- REPLY TO CLIENT ---
            if is_https:
                # HTTPS: Send 200 Connection Established
                client_socket.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                # HTTP: Forward the original request to the remote
                remote_socket.sendall(request)

            # --- PIPE DATA ---
            client_socket.settimeout(None)
            remote_socket.settimeout(None)
            self.pipe_sockets(client_socket, remote_socket)

        except Exception:
            pass 
        finally:
            try: 
                client_socket.close()
            except: pass
            
            try: 
                if remote_socket: remote_socket.close()
            except: pass

    def pipe_sockets(self, s1, s2):
        try:
            sockets = [s1, s2]
            while True:
                r, _, _ = select.select(sockets, [],[], 20)
                if not r: break
                for sock in r:
                    # Reduced to 32KB to prevent USB Wi-Fi driver crashes / Buffer overflows
                    data = sock.recv(32768) 
                    if not data: return
                    if sock is s1: s2.sendall(data)
                    else: s1.sendall(data)
        except: pass

active_proxies = {}

def start_proxy_for_interface(interface_name, port):
    """Starts a proxy strictly locked to a specific interface NAME, not an IP."""
    key = f"{interface_name}:{port}"
    if key in active_proxies: return
    proxy = InterfaceProxy(interface_name, port)
    proxy.start()
    active_proxies[key] = proxy

def stop_all_proxies():
    for p in active_proxies.values(): p.stop()
    active_proxies.clear()