import subprocess
import json
import time
import sys
import argparse
import logging

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("mcp_dblp_client.log"),
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("mcp_dblp_client")

def run_mcp_call(tool, arguments, debug=False):
    """
    Run an MCP call to the server with the specified tool and arguments.
    
    Args:
        tool (str): The name of the tool to call
        arguments (dict): The arguments to pass to the tool
        debug (bool): Whether to print debug information
    """
    process = subprocess.Popen(
        ["python", "src/mcp_dblp/server.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )
    
    # Wait for server to start
    logger.info("Waiting for server to start...")
    time.sleep(0.5)
    
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "call_tool",
        "params": {
            "name": tool,
            "arguments": arguments
        }
    }
    
    # Prepare the request with Content-Length header
    json_content = json.dumps(message)
    content_length = len(json_content)
    request = f"Content-Length: {content_length}\r\n\r\n{json_content}"
    
    logger.info(f"Sending request for '{tool}' with arguments: {arguments}")
    
    if debug:
        logger.debug(f"Raw request:\n{request}")
    
    try:
        # Send the request
        process.stdin.write(request)
        process.stdin.flush()
        
        # Read the response
        headers = {}
        header_data = ""
        
        # Read headers
        while True:
            line = process.stdout.readline()
            header_data += line
            if debug:
                logger.debug(f"Header line: {line!r}")
            
            line = line.strip()
            if not line:
                break
            
            parts = line.split(':', 1)
            if len(parts) == 2:
                headers[parts[0].strip().lower()] = parts[1].strip()
        
        if debug:
            logger.debug(f"Received headers: {headers}")
        
        content_length = int(headers.get('content-length', 0))
        if content_length > 0:
            content = process.stdout.read(content_length)
            logger.info(f"Response received, length: {len(content)}")
            if debug:
                logger.debug(f"Raw response content: {content}")
            
            try:
                parsed = json.loads(content)
                if debug:
                    logger.debug(f"Parsed JSON: {json.dumps(parsed, indent=2)}")
                print("\nParsed Response:")
                print(json.dumps(parsed, indent=2))
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON: {e}")
                print("\nResponse (raw):")
                print(content)
        else:
            logger.warning("No content length specified in response headers")
            print("\nNo response content received")
        
        # Check for server errors
        stderr_output = process.stderr.read()
        if stderr_output:
            logger.warning(f"Server stderr output: {stderr_output}")
            print("\nServer Error Output:")
            print(stderr_output)
        
    except Exception as e:
        logger.exception(f"Error during MCP call: {e}")
        print(f"Error: {e}")
    finally:
        # Terminate the server process
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            logger.warning("Had to forcefully kill the server process")

def list_tools():
    """List all available tools on the server"""
    process = subprocess.Popen(
        ["python", "src/mcp_dblp/server.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )
    
    time.sleep(0.5)
    
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {}
    }
    
    json_content = json.dumps(message)
    content_length = len(json_content)
    request = f"Content-Length: {content_length}\r\n\r\n{json_content}"
    
    try:
        process.stdin.write(request)
        process.stdin.flush()
        
        headers = {}
        while True:
            line = process.stdout.readline().strip()
            if not line:
                break
            parts = line.split(':', 1)
            if len(parts) == 2:
                headers[parts[0].strip().lower()] = parts[1].strip()
        
        content_length = int(headers.get('content-length', 0))
        content = process.stdout.read(content_length) if content_length > 0 else ""
        
        print("\nAvailable Tools:")
        if content:
            try:
                response = json.loads(content)
                tools = response.get("result", {}).get("tools", [])
                for tool in tools:
                    print(f"  - {tool['name']}: {tool['description']}")
            except json.JSONDecodeError:
                print("Error parsing response")
        else:
            print("No tools returned")
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        process.terminate()

def main():
    parser = argparse.ArgumentParser(description="MCP DBLP Client")
    parser.add_argument("--list-tools", action="store_true", help="List available tools")
    parser.add_argument("--tool", help="Tool to call")
    parser.add_argument("--args", help="JSON string of arguments for the tool")
    parser.add_argument("--debug", action="store_true", help="Enable debug output")
    
    args = parser.parse_args()
    
    if args.list_tools:
        list_tools()
    elif args.tool:
        tool_args = {}
        if args.args:
            try:
                tool_args = json.loads(args.args)
            except json.JSONDecodeError as e:
                print(f"Error parsing arguments JSON: {e}")
                return
        
        run_mcp_call(args.tool, tool_args, debug=args.debug)
    else:
        # Default behavior
        print("Running echo tool as a test...")
        run_mcp_call("echo", {"message": "Hello, DBLP!"}, debug=args.debug)

if __name__ == "__main__":
    main()