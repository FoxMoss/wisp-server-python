import asyncio
import struct

from websockets.server import serve
from websockets.exceptions import ConnectionClosed

tcp_size = 64*1024
queue_size = 128

#wisp packet format definitions
#see https://docs.python.org/3/library/struct.html for what these characters mean
packet_format = "<BI"
connect_format = "<BH"
continue_format = "<B"
close_format = "<B"

class WSProxyConnection:
  def __init__(self, ws, path):
    self.ws = ws
    self.path = path

  async def setup_connection(self):
    addr_str = self.path.split("/")[-1]
    self.tcp_host, self.tcp_port = addr_str.split(":")
    self.tcp_port = int(self.tcp_port)

    self.tcp_reader, self.tcp_writer = await asyncio.open_connection(host=self.tcp_host, port=self.tcp_port, limit=tcp_size)

  async def handle_ws(self):
    while True:
      try:
        data = await self.ws.recv()
      except ConnectionClosed:
        break
      self.tcp_writer.write(data)
      await self.tcp_writer.drain()
    
    self.tcp_writer.close()
  
  async def handle_tcp(self):
    while True:
      data = await self.tcp_reader.read(tcp_size)
      if len(data) == 0:
        break #socket closed
      await self.ws.send(data)
    
    await self.ws.close()

class WispConnection:
  def __init__(self, ws, path):
    self.ws = ws
    self.path = path
    self.active_streams = {}
  
  #send the initial CONTINUE packet
  async def setup(self):
    continue_payload = struct.pack(continue_format, queue_size)
    continue_packet = struct.pack(packet_format, 0x03, 0) + continue_payload
    await self.ws.send(continue_packet)

  async def new_stream(self, stream_id, payload):
    stream_type, destination_port = struct.unpack(connect_format, payload[:3])
    hostname = payload[3:].decode()
    
    if stream_type != 1: #udp not supported yet
      close_payload = struct.pack(close_format, 0x41)
      close_packet = struct.pack(packet_format, 0x04, stream_id) + close_payload
      await self.ws.send(close_packet)
      return
    
    tcp_reader, tcp_writer = await asyncio.open_connection(host=hostname, port=destination_port, limit=tcp_size)
    self.active_streams[stream_id]["reader"] = tcp_reader
    self.active_streams[stream_id]["writer"] = tcp_writer

    ws_to_tcp_task = asyncio.create_task(self.task_wrapper(self.stream_ws_to_tcp, stream_id))
    tcp_to_ws_task = asyncio.create_task(self.task_wrapper(self.stream_tcp_to_ws, stream_id))
    asyncio.gather(ws_to_tcp_task, tcp_to_ws_task)
  
  async def task_wrapper(self, target_func, *args, **kwargs):
    try:
      await target_func(*args, **kwargs)
    except asyncio.CancelledError as e:
      raise e
  
  async def stream_ws_to_tcp(self, stream_id):
    #this infinite loop should get killed by the task.cancel call later on
    while True: 
      stream = self.active_streams[stream_id]
      data = await stream["queue"].get()
      stream["writer"].write(data)
      await stream["writer"].drain()

      #send a CONTINUE packet periodically
      stream["packets_sent"] += 1
      if stream["packets_sent"] % queue_size / 4 == 0:
        buffer_remaining = stream["queue"].maxsize - stream["queue"].qsize()
        continue_payload = struct.pack(continue_format, buffer_remaining)
        continue_packet = struct.pack(packet_format, 0x03, stream_id) + continue_payload
        await self.ws.send(continue_packet)
  
  async def stream_tcp_to_ws(self, stream_id):
    while True:
      stream = self.active_streams[stream_id]
      data = await stream["reader"].read(tcp_size)
      if len(data) == 0: #connection closed
        self.close_stream(stream_id)
        break
      data_packet = struct.pack(packet_format, 0x02, stream_id) + data
      await self.ws.send(data_packet)
    
    #send close packet to ws
    if not stream_id in self.active_streams:
      return
    close_payload = struct.pack(close_format, 0x01)
    close_packet = struct.pack(packet_format, 0x04, stream_id) + close_payload
    await self.ws.send(close_packet)
  
  def close_stream(self, stream_id):
    if not stream_id in self.active_streams:
      return #stream already closed
    stream = self.active_streams[stream_id]
    self.close_tcp(stream["writer"])

    #kill the running tasks associated with this stream
    if not stream["connect_task"].done():
      stream["connect_task"].cancel() 
    if stream["ws_to_tcp_task"] is not None and not stream["ws_to_tcp_task"].done():
      stream["ws_to_tcp_task"].cancel()
    if stream["tcp_to_ws_task"] is not None and not stream["tcp_to_ws_task"].done():
      stream["tcp_to_ws_task"].cancel()
    
    del self.active_streams[stream_id]
  
  def close_tcp(self, tcp_writer):
    if tcp_writer is None:
      return
    if tcp_writer.is_closing():
      return
    tcp_writer.close()
  
  async def handle_ws(self):
    while True:
      try:
        data = await self.ws.recv()
      except ConnectionClosed:
        break
      
      #get basic packet info
      payload = data[5:]
      packet_type, stream_id = struct.unpack(packet_format, data[:5])

      if packet_type == 0x01: #CONNECT packet
        connect_task = asyncio.create_task(self.task_wrapper(self.new_stream, stream_id, payload))
        self.active_streams[stream_id] = {
          "reader": None,
          "writer": None,
          "queue": asyncio.Queue(queue_size),
          "connect_task": connect_task,
          "ws_to_tcp_task": None,
          "tcp_to_ws_task": None,
          "packets_sent": 0
        }
        asyncio.gather(connect_task)
      
      elif packet_type == 0x02: #DATA packet
        stream = self.active_streams[stream_id]
        await stream["queue"].put(payload)
      
      elif packet_type == 0x04: #CLOSE packet
        reason = struct.unpack(close_format, payload)[0]
        self.close_stream(stream_id)
        print("stream closed with reason " + hex(reason))
  
    #close all active streams when the websocket disconnects
    for stream_id in list(self.active_streams.keys()):
      self.close_stream(stream_id)

async def connection_handler(websocket, path):
  print("incoming connection from "+path)
  if path.endswith("/"):
    connection = WispConnection(websocket, path)
    await connection.setup()
    ws_handler = asyncio.create_task(connection.handle_ws())  
    await asyncio.gather(ws_handler)

  else:
    connection = WSProxyConnection(websocket, path)
    await connection.setup_connection()
    ws_handler = asyncio.create_task(connection.handle_ws())
    tcp_handler = asyncio.create_task(connection.handle_tcp())
    await asyncio.gather(ws_handler, tcp_handler)

async def main():
  async with serve(connection_handler, "127.0.0.1", 6001, subprotocols=["wisp-v1"]):
    await asyncio.Future()

if __name__ == "__main__":
  asyncio.run(main())