// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "../src/interfaces/ILiquidatorInterfaces.sol";

contract MockERC20 is IERC20 {
    string public name;
    string public symbol;
    uint8 public immutable override decimals;
    uint256 public override totalSupply;
    mapping(address => uint256) public override balanceOf;
    mapping(address => mapping(address => uint256)) public override allowance;

    constructor(string memory _name, string memory _symbol, uint8 _decimals) {
        name = _name;
        symbol = _symbol;
        decimals = _decimals;
    }

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
        totalSupply += amount;
    }

    function transfer(address recipient, uint256 amount) external override returns (bool) {
        require(balanceOf[msg.sender] >= amount, "ERC20: insufficient balance");
        balanceOf[msg.sender] -= amount;
        balanceOf[recipient] += amount;
        return true;
    }

    function approve(address spender, uint256 amount) external override returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transferFrom(address sender, address recipient, uint256 amount) external override returns (bool) {
        require(balanceOf[sender] >= amount, "ERC20: insufficient balance");
        require(allowance[sender][msg.sender] >= amount, "ERC20: insufficient allowance");
        allowance[sender][msg.sender] -= amount;
        balanceOf[sender] -= amount;
        balanceOf[recipient] += amount;
        return true;
    }
}

contract MockBalancerVault is IBalancerVault {
    uint256 public feeBps = 0; // Balancer has 0% flash loan fee on Base

    function flashLoan(
        address recipient,
        IERC20[] memory tokens,
        uint256[] memory amounts,
        bytes memory userData
    ) external override {
        uint256[] memory feeAmounts = new uint256[](tokens.length);
        for (uint256 i = 0; i < tokens.length; i++) {
            feeAmounts[i] = (amounts[i] * feeBps) / 10000;
            // Transfer flash loaned funds to recipient
            tokens[i].transfer(recipient, amounts[i]);
        }

        // Call callback on recipient
        IFlashLoanRecipient(recipient).receiveFlashLoan(tokens, amounts, feeAmounts, userData);

        // Verify repayment received
        // In real balancer, vault checks its ending balance >= starting balance + fee
    }
}

contract MockCToken is ICToken {
    address public override underlying;
    uint256 public seizeMultiplier = 110; // 10% liquidation bonus
    uint256 public override exchangeRateStored = 1e18;
    mapping(address => uint256) public override balanceOf;

    constructor(address _underlying) {
        underlying = _underlying;
    }

    function liquidateBorrow(
        address /* borrower */,
        uint256 repayAmount,
        address cTokenCollateral
    ) external override returns (uint256) {
        // Collect debt repayment from liquidator
        IERC20(underlying).transferFrom(msg.sender, address(this), repayAmount);
        
        // Mint collateral cTokens to liquidator with 10% bonus
        uint256 cTokenSeized = (repayAmount * seizeMultiplier) / 100;
        MockCToken(cTokenCollateral).mintSeized(msg.sender, cTokenSeized);
        return 0; // Success code 0
    }

    function mintSeized(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function redeem(uint256 redeemTokens) external override returns (uint256) {
        require(balanceOf[msg.sender] >= redeemTokens, "INSUFFICIENT_CTOKEN");
        balanceOf[msg.sender] -= redeemTokens;
        // Transfer underlying collateral to redeemer
        MockERC20(underlying).mint(msg.sender, redeemTokens);
        return 0;
    }
}

contract MockSwapRouter is ISwapRouter {
    uint256 public simulatedProceeds;

    function setProceeds(uint256 proceeds) external {
        simulatedProceeds = proceeds;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external payable override returns (uint256) {
        // Pull input collateral token
        IERC20(params.tokenIn).transferFrom(msg.sender, address(this), params.amountIn);
        
        uint256 out = simulatedProceeds > 0 ? simulatedProceeds : params.amountIn;
        require(out >= params.amountOutMinimum, "SLIPPAGE_EXCEEDED");
        
        // Mint or transfer output debt token to recipient
        MockERC20(params.tokenOut).mint(params.recipient, out);
        return out;
    }
}
