import socket

# Setting the server IPv4 address and the port number
HOST = "127.0.0.1"
PORT = 50007

# Creating TCP socket
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:

    # Connecting to server
    client_socket.connect((HOST, PORT))

    print(f"Connected to server at {HOST}:{PORT}")

    while True:

        # Prompting the user for a message
        message = input("Enter message: ")

        # Sending the encoded message to the server (in bytes, not str)
        client_socket.sendall(message.encode())

        # Allow disconnect through "exit" message
        if message.lower() == "exit":
            print("Closing client.")
            break

        # Receive server acknowledgement
        data = client_socket.recv(1024)

        # Printing the server acknowledgement
        print("Server response:", data.decode())