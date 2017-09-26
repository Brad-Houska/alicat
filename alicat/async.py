"""
A Python driver for Alicat mass flow controllers, using asynchronous TCP
communication.

Distributed under the GNU General Public License v2
Copyright (C) 2017 NuMat Technologies
"""
try:
    import asyncio
except ImportError:
    raise ImportError("TCP connections require python >=3.5.")

import logging


class FlowMeter(object):
    """Python driver for [Alicat Flow Meters](http://www.alicat.com/
    products/mass-flow-meters-and-controllers/mass-flow-meters/).

    This communicates with the flow meter over a TCP bridge such as the
    [StarTech Device Server](https://www.startech.com/Networking-IO/
    Serial-over-IP/4-Port-Serial-Ethernet-Device-Server-with-PoE~NETRS42348PD).
    """
    def __init__(self, ip, port, address='A'):
        """Initializes the device client.

        Args:
            ip: IP address of the device server
            port: Port of the device server
            address: The Alicat-specified address, A-Z. Default 'A'.
        """
        self.ip = ip
        self.port = port
        self.address = address
        self.open = False
        self.reconnecting = False
        self.timeouts = 0
        self.max_timeouts = 10
        self.waiting = False
        self.keys = ['pressure', 'temperature', 'volumetric_flow', 'mass_flow',
                     'flow_setpoint', 'gas']
        self.gases = ['Air', 'Ar', 'CH4', 'CO', 'CO2', 'C2H6', 'H2', 'He',
                      'N2', 'N2O', 'Ne', 'O2', 'C3H8', 'n-C4H10', 'C2H2',
                      'C2H4', 'i-C2H10', 'Kr', 'Xe', 'SF6', 'C-25', 'C-10',
                      'C-8', 'C-2', 'C-75', 'A-75', 'A-25', 'A1025', 'Star29',
                      'P-5']

    async def _connect(self):
        """Asynchronously opens a TCP connection with the server."""
        self.open = False
        reader, writer = await asyncio.open_connection(self.ip, self.port)
        self.connection = {'reader': reader, 'writer': writer}
        self.open = True

    async def get(self):
        """Get the current state of the flow controller.

        From the Alicat mass flow controller documentation, this data is:
         * Pressure (normally in psia)
         * Temperature (normally in C)
         * Volumetric flow (in units specified at time of order)
         * Mass flow (in units specified at time of order)
         * Flow setpoint (only for flow controllers)
         * Total flow (only on models with the optional totalizer function)
         * Currently selected gas

        Args:
            retries: Number of times to re-attempt reading. Default 2.
        Returns:
            The state of the flow controller, as a dictionary.
        """
        command = '*@={addr}\r'.format(addr=self.address)
        line = await self._write_and_read(command)
        if line:
            spl = line.split()
            address, values = spl[0], spl[1:]

            # Mass/volume over range error.
            # Explicitly silenced because I find it redundant.
            while values[-1] in ['MOV', 'VOV']:
                del values[-1]

            if address != self.address:
                raise ValueError("Flow controller address mismatch.")
            if len(values) == 5 and len(self.keys) == 6:
                del self.keys[-2]
            elif len(values) == 7 and len(self.keys) == 6:
                self.keys.insert(5, 'total flow')
            return {k: (v if k == self.keys[-1] else float(v))
                    for k, v in zip(self.keys, values)}
        else:
            return None

    async def set_gas(self, gas):
        """Sets the gas type.

        Args:
            gas: The gas type, as a string. Supported gas types are:
                'Air', 'Ar', 'CH4', 'CO', 'CO2', 'C2H6', 'H2', 'He', 'N2',
                'N2O', 'Ne', 'O2', 'C3H8', 'n-C4H10', 'C2H2', 'C2H4',
                'i-C2H10', 'Kr', 'Xe', 'SF6', 'C-25', 'C-10', 'C-8', 'C-2',
                'C-75', 'A-75', 'A-25', 'A1025', 'Star29', 'P-5'
        """
        if gas not in self.gases:
            raise ValueError("{} not supported!".format(gas))
        command = '{addr}$${gas}\r'.format(addr=self.address,
                                           gas=self.gases.index(gas))
        line = await self._write_and_read(command)
        if line.split()[-1] != gas:
            raise IOError("Could not set gas type")

    def close(self):
        """Closes the device server connection."""
        if self.open:
            self.connection['writer'].close()
        self.open = False

    async def _write_and_read(self, command):
        """Writes a command and reads a response from the flow controller.

        There are two fail points here: 1. communication between this driver
        and the proxy, and 2. communication between the proxy and the Alicat.
        We need to separately check for and manage each.
        """
        if self.waiting:
            return None
        self.waiting = True

        try:
            if not self.open:
                await asyncio.wait_for(self._connect(), timeout=0.5)
            self.reconnecting = False
        except asyncio.TimeoutError:
            if not self.reconnecting:
                logging.error('Connecting to {}:{} timed out.'.format(
                              self.ip, self.port))
            self.reconnecting = True
            return None

        try:
            self.connection['writer'].write(command.encode())
            future = self.connection['reader'].readuntil(b'\r')
            line = await asyncio.wait_for(future, timeout=0.9)
            result = line.decode().strip()
            self.timeouts = 0
        except asyncio.TimeoutError:
            self.timeouts += 1
            if self.timeouts == self.max_timeouts:
                logging.error('Reading Alicat from {}:{} timed out {} times.'
                              .format(self.ip, self.port, self.max_timeouts))
            result = None
        self.waiting = False
        return result


class FlowController(FlowMeter):
    """Python driver for [Alicat Flow Controllers](http://www.alicat.com/
    products/mass-flow-meters-and-controllers/mass-flow-controllers/).

    This communicates with the flow controller over a USB or RS-232/RS-485
    connection using pyserial.

    To set up your Alicat flow controller, power on the device and make sure
    that the "Input" option is set to "Serial".
    """
    async def set_flow_rate(self, flow, retries=2):
        """Sets the target flow rate.

        Args:
            flow: The target flow rate, in units specified at time of purchase
        """
        command = '{addr}S{flow:.2f}\r'.format(addr=self.address, flow=flow)
        line = await self._write_and_read(command)

        # Some Alicat models don't return the setpoint. This accounts for
        # these devices.
        try:
            setpoint = float(line.split()[-2])
        except (IndexError, AttributeError) as e:
            setpoint = None

        if setpoint is not None and abs(setpoint - flow) > 0.01:
            raise IOError("Could not set flow.")


def command_line(args):
    import json
    from time import time

    ip, port = args.port[6:].split(':')
    port = int(port)
    flow_controller = FlowController(ip, port, args.address)

    async def print_state():
        if args.set_gas:
            await flow_controller.set_gas(args.set_gas)
        if args.set_flow_rate is not None:
            await flow_controller.set_flow_rate(args.set_flow_rate)
        state = await flow_controller.get()
        if args.stream:
            print('time\t' + '\t'.join(flow_controller.keys))
            t0 = time()
            while True:
                state = await flow_controller.get()
                print('{:.2f}\t'.format(time() - t0) +
                      '\t\t'.join('{:.2f}'.format(state[key])
                                  for key in flow_controller.keys[:-1]) +
                      '\t\t' + state['gas'])
        else:
            print(json.dumps(state, indent=2, sort_keys=True))

    ioloop = asyncio.get_event_loop()
    try:
        ioloop.run_until_complete(print_state())
    except KeyboardInterrupt:
        pass
    flow_controller.close()
    ioloop.close()
