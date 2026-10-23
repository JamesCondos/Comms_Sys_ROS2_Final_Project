r"""Bare-bones full topology, using ROS topics for every connection.

Robot 1 + gateway --\
                    Edge A -> Site link A --\
Robot 2 + gateway --/                        \
                                             Load balancer -> Workers 1, 2
Robot 3 + gateway --\                        /
                    Edge B -> Site link B --/
Robot 4 + gateway --/

Gateways/edges are pass-through placeholders, not REST implementations.
Site-link nodes represent the router positions; they are ROS relays, not IP routers.
"""
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    nodes = []
    for site, robots in (('site_a', (1, 2)), ('site_b', (3, 4))):
        gateway_topics = []
        for number in robots:
            robot_id = f'robot_{number}'
            namespace = f'/{site}/{robot_id}'
            uplink = namespace + '/uplink'
            gateway_topics.append(uplink)
            nodes.append(Node(
                package='complex_network', executable='robot', name='source',
                namespace=namespace, output='screen',
                parameters=[{'robot_id': robot_id, 'publish_rate_hz': 1.0}],
            ))
            nodes.append(Node(
                package='complex_network', executable='relay', name='gateway',
                namespace=namespace, output='screen',
                parameters=[{'input_topics': [namespace + '/telemetry'],
                             'output_topic': uplink}],
            ))
        nodes.append(Node(
            package='complex_network', executable='relay', name='edge',
            namespace='/' + site, output='screen',
            parameters=[{'input_topics': gateway_topics,
                         'output_topic': f'/{site}/edge/uplink'}],
        ))
        nodes.append(Node(
            package='complex_network', executable='relay', name='site_link_stub',
            namespace='/' + site, output='screen',
            parameters=[{'input_topics': [f'/{site}/edge/uplink'],
                         'output_topic': '/central/ingress'}],
        ))

    nodes.append(Node(
        package='complex_network', executable='load_balancer',
        name='load_balancer', namespace='/central', output='screen',
    ))
    for number in (1, 2):
        nodes.append(Node(
            package='complex_network', executable='worker', name='worker',
            namespace=f'/central/worker_{number}', output='screen',
        ))
    return LaunchDescription(nodes)
