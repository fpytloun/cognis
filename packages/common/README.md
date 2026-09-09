# cognis-common

Shared contracts and portable runtimes used by the Cognis controller and
executor.

The controller and executor publish separate distributions. Install
`cognis-common` only when developing or packaging the two components; normal
users install `cognis-controller` and, when local tool execution is required,
`cognis-executor[full]`.

The package also owns the portable MCP client runtime. Controller policy and
executor process lifecycle remain in their respective distributions.
